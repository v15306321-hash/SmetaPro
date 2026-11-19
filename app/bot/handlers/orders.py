"""Калькулятор: создание заказа, позиции, наценки, PDF-смета, статусы, учёт расхода/отходов."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.core import BTN_NEW, BTN_ORDERS, answer, call, e, kb, parse_num
from app.core.errors import ValidationError
from app.core.fmt import money, qty
from app.models import ItemKind, OrderStatus
from app.services import estimates, materials, orders, tariffs, waste
from app.services.accounts import Ctx

router = Router(name="orders")
PAGE = 8
KIND_LABEL = {ItemKind.material: "материал", ItemKind.hardware: "фурнитуру", ItemKind.work: "работу"}


class NewOrder(StatesGroup):
    client = State()
    phone = State()
    title = State()


class Item(StatesGroup):
    manual = State()     # «название; ед.; цена»
    work = State()       # «название; цена[; кол-во; ед.]»
    amount = State()     # размеры / количество
    markups = State()
    waste = State()


# ---------------------------------------------------------------- карточка заказа

def order_card(ctx: Ctx, order_id: int) -> tuple[str, dict]:
    o = orders.get(ctx, order_id)
    statuses = tariffs.has_feature(ctx.db, ctx.eid, "order_statuses")
    lines = [f"📋 <b>Заказ №{o.number}</b>" + (f" — {e(o.title)}" if o.title else "")]
    client = ", ".join(x for x in [o.client_name, o.client_phone] if x)
    if client:
        lines.append(f"👤 {e(client)}")
    if statuses:
        lines.append(f"Статус: <b>{orders.STATUS_TITLES[o.status]}</b>")
    if o.items:
        lines.append("")
        for i, it in enumerate(o.items, 1):
            size = f" {it.height_mm}×{it.width_mm}×{qty(it.pieces)}шт" if it.height_mm and it.width_mm else ""
            lines.append(f"{i}. {e(it.name)}{size} — {qty(it.quantity)} {e(it.unit)} × {money(it.unit_cost, '')}"
                         f" = {money(it.cost, '')} → <b>{money(it.price, '')}</b>")
    else:
        lines.append("\nПозиций пока нет — добавьте материалы, фурнитуру и работы.")
    lines += ["",
              f"Наценки: материалы {qty(o.markup_materials)}% · фурнитура {qty(o.markup_hardware)}% · "
              f"работы {qty(o.markup_works)}%" + (f" · фикс. {money(o.fixed_addition)}" if o.fixed_addition else ""),
              f"Себестоимость: {money(o.cost_total)}",
              f"Доход от наценок: {money(o.markup_income)}",
              f"💰 <b>Итого клиенту: {money(o.price_total)}</b>"]
    return "\n".join(lines), dict(id=o.id, statuses=statuses, can_edit=ctx.can("orders.edit"),
                                  can_pdf=ctx.can("estimates.create"), can_status=ctx.can("orders.status"),
                                  waste=tariffs.has_feature(ctx.db, ctx.eid, "waste") and ctx.can("waste.edit"),
                                  has_items=bool(o.items))


def card_kb(info: dict):
    oid = info["id"]
    rows = []
    if info["can_edit"]:
        rows += [[("➕ Материал", f"oi:pick:{oid}:material:0"), ("➕ Фурнитура", f"oi:pick:{oid}:hardware:0")],
                 [("➕ Работа цеха", f"oi:work:{oid}"), ("✍️ Своя позиция", f"oi:manual:{oid}")]]
        if info["has_items"]:
            rows.append([("🗑 Удалить позицию", f"oi:rmlist:{oid}"), ("📐 Наценки", f"o:mk:{oid}")])
    if info["can_pdf"] and info["has_items"]:
        rows.append([("📄 PDF-смета", f"o:pdf:{oid}")])
    extra = []
    if info["statuses"] and info["can_status"]:
        extra.append(("🔄 Статус", f"o:st:{oid}"))
    if info["waste"] and info["has_items"]:
        extra.append(("♻️ Расход и отходы", f"w:show:{oid}"))
    rows.append(extra)
    rows.append([("🔙 К заказам", "o:list:0")])
    return kb(*rows)


async def show_card(event, order_id: int, edit: bool = False):
    res = await call(event, lambda ctx: order_card(ctx, order_id))
    if res:
        text, info = res
        await answer(event, text, card_kb(info), edit=edit)


# ---------------------------------------------------------------- новый заказ

@router.message(F.text == BTN_NEW)
async def new_order(message: Message, state: FSMContext):
    ok = await call(message, lambda ctx: (ctx.require("orders.edit"), tariffs.require_active(ctx.db, ctx.eid)))
    if ok is None:
        return
    await state.set_state(NewOrder.client)
    await message.answer("🧮 Новый расчёт.\nИмя клиента? (или «-»)")


@router.message(NewOrder.client, F.text)
async def no_client(message: Message, state: FSMContext):
    await state.update_data(client="" if message.text.strip() == "-" else message.text.strip()[:200])
    await state.set_state(NewOrder.phone)
    await message.answer("Телефон клиента? (или «-»)")


@router.message(NewOrder.phone, F.text)
async def no_phone(message: Message, state: FSMContext):
    await state.update_data(phone=None if message.text.strip() == "-" else message.text.strip()[:32])
    await state.set_state(NewOrder.title)
    await message.answer("Что изготавливаем? Например: Кухня, Шкаф-купе. Это название раздела в смете.")


@router.message(NewOrder.title, F.text)
async def no_title(message: Message, state: FSMContext):
    data = await state.get_data()
    title = message.text.strip()[:200]
    oid = await call(message, lambda ctx: orders.create(ctx, title=title, client_name=data["client"],
                                                       client_phone=data["phone"]).id)
    await state.clear()
    if oid:
        await show_card(message, oid)


# ---------------------------------------------------------------- список

@router.message(F.text == BTN_ORDERS)
async def list_orders_msg(message: Message):
    await _list(message, 0)


@router.callback_query(F.data.startswith("o:list:"))
async def list_orders_cb(cq: CallbackQuery):
    await _list(cq, int(cq.data.split(":")[2]), edit=True)


async def _list(event, page: int, edit=False):
    def work(ctx):
        items = orders.list_orders(ctx, limit=PAGE + 1, offset=page * PAGE)
        return [(o.id, f"№{o.number} {o.client_name or o.title or ''} · {money(o.price_total, '')}"[:60]) for o in items]
    rows = await call(event, work)
    if rows is None:
        return
    if not rows and page == 0:
        await answer(event, "Заказов пока нет. Нажмите «🧮 Новый расчёт».")
        return
    buttons = [[(t, f"o:open:{oid}")] for oid, t in rows[:PAGE]]
    nav = []
    if page:
        nav.append(("◀️", f"o:list:{page - 1}"))
    if len(rows) > PAGE:
        nav.append(("▶️", f"o:list:{page + 1}"))
    await answer(event, "📋 Заказы (новые сверху):", kb(*buttons, nav), edit=edit)


@router.callback_query(F.data.startswith("o:open:"))
async def open_order(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_card(cq, int(cq.data.split(":")[2]))


# ---------------------------------------------------------------- добавление позиций

@router.callback_query(F.data.startswith("oi:pick:"))
async def pick_material(cq: CallbackQuery):
    _, _, oid, kind, page = cq.data.split(":")
    oid, page, kind = int(oid), int(page), ItemKind(kind)

    def work(ctx):
        return [(m.id, f"{m.name} · {money(m.price, '')}/{m.unit}"[:60])
                for m in materials.list_materials(ctx, kind=kind)]
    mats = await call(cq, work)
    if mats is None:
        return
    chunk = mats[page * PAGE:(page + 1) * PAGE]
    rows = [[(t, f"oi:m:{oid}:{mid}")] for mid, t in chunk]
    nav = []
    if page:
        nav.append(("◀️", f"oi:pick:{oid}:{kind.value}:{page - 1}"))
    if len(mats) > (page + 1) * PAGE:
        nav.append(("▶️", f"oi:pick:{oid}:{kind.value}:{page + 1}"))
    rows += [nav, [("✍️ Ввести вручную", f"oi:manual:{oid}"), ("🔙 Заказ", f"o:open:{oid}")]]
    title = "Выберите материал:" if kind == ItemKind.material else "Выберите фурнитуру:"
    if not mats:
        title = "В базе пока нет позиций этого типа. Добавьте их в «📦 Материалы» или введите вручную."
    await answer(cq, title, kb(*rows), edit=True)


def _amount_prompt(unit: str) -> str:
    if unit == "м²":
        return ("Укажите размеры детали в мм и количество штук: <code>В×Ш×шт</code>, например <code>720x600x2</code>.\n"
                "Или площадь в м²: <code>4,5</code>")
    if unit == "м.п.":
        return "Укажите длину в мм и количество: <code>2500x3</code>, или погонные метры: <code>7,5</code>"
    return f"Количество ({e(unit)}):"


@router.callback_query(F.data.startswith("oi:m:"))
async def material_chosen(cq: CallbackQuery, state: FSMContext):
    _, _, oid, mid = cq.data.split(":")
    res = await call(cq, lambda ctx: (lambda m: (m.name, m.unit))(materials.get(ctx, int(mid))))
    if not res:
        return
    await state.set_state(Item.amount)
    await state.update_data(order_id=int(oid), material_id=int(mid), unit=res[1])
    await answer(cq, f"<b>{e(res[0])}</b>\n{_amount_prompt(res[1])}")


@router.callback_query(F.data.startswith("oi:manual:"))
async def manual_item(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Item.manual)
    await state.update_data(order_id=int(cq.data.split(":")[2]))
    await answer(cq, "Введите позицию: <code>Название; единица; цена за единицу; тип</code>\n"
                     "тип: м — материал, ф — фурнитура (по умолчанию м).\n"
                     "Пример: <code>Зеркало 4 мм; м²; 180000</code>")


@router.message(Item.manual, F.text)
async def manual_entered(message: Message, state: FSMContext):
    parts = [p.strip() for p in message.text.split(";")]
    if len(parts) < 3 or not parts[0]:
        await message.answer("Формат: <code>Название; единица; цена</code>. Попробуйте ещё раз или /cancel.")
        return
    try:
        price = parse_num(parts[2])
    except Exception:
        await message.answer("Цена должна быть числом. Попробуйте ещё раз.")
        return
    kind = ItemKind.hardware if len(parts) > 3 and parts[3].lower().startswith("ф") else ItemKind.material
    await state.update_data(name=parts[0][:200], unit=parts[1][:16] or "шт", unit_cost=str(price), kind=kind.value,
                            material_id=None)
    await state.set_state(Item.amount)
    await message.answer(_amount_prompt(parts[1]))


_SIZE = re.compile(r"^\s*(\d{1,6})\s*[xх×*]\s*(\d{1,6})(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?\s*$", re.I)
_LEN = re.compile(r"^\s*(\d{1,6})\s*[xх×*]\s*(\d+(?:[.,]\d+)?)\s*$", re.I)


def parse_amount(unit: str, text: str) -> dict:
    t = text.strip()
    if unit == "м²":
        m = _SIZE.match(t)
        if m:
            return dict(height_mm=int(m.group(1)), width_mm=int(m.group(2)), pieces=m.group(3) or "1")
    if unit == "м.п.":
        m = _LEN.match(t)
        if m:
            return dict(width_mm=int(m.group(1)), pieces=m.group(2))
    return dict(quantity=parse_num(t))


@router.message(Item.amount, F.text)
async def amount_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = parse_amount(data["unit"], message.text)
    except Exception:
        await message.answer("Не понял значение. " + _amount_prompt(data["unit"]))
        return

    def work(ctx):
        o = orders.get(ctx, data["order_id"])
        kw = dict(section=o.title or "Изделие", **amount)
        if data.get("material_id"):
            orders.add_item(ctx, o.id, kind=ItemKind.material, material_id=data["material_id"], **kw)
        else:
            orders.add_item(ctx, o.id, kind=ItemKind(data["kind"]), name=data["name"], unit=data["unit"],
                            unit_cost=data["unit_cost"], **kw)
        return o.id
    oid = await call(message, work)
    if oid:
        await state.clear()
        await show_card(message, oid)


@router.callback_query(F.data.startswith("oi:work:"))
async def work_item(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Item.work)
    await state.update_data(order_id=int(cq.data.split(":")[2]))
    await answer(cq, "Работа цеха (сборка, монтаж и т.п. — учитываются здесь, с наценкой на работы):\n"
                     "<code>Название; стоимость[; количество; единица]</code>\n"
                     "Примеры: <code>Сборка и монтаж; 900000</code> · <code>Монтаж столешницы; 80000; 3; м.п.</code>")


@router.message(Item.work, F.text)
async def work_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    parts = [p.strip() for p in message.text.split(";")]
    try:
        if len(parts) < 2 or not parts[0]:
            raise ValidationError("format")
        cost = parse_num(parts[1])
        quantity = parse_num(parts[2]) if len(parts) > 2 and parts[2] else 1
        unit = parts[3][:16] if len(parts) > 3 and parts[3] else "услуга"
    except Exception:
        await message.answer("Формат: <code>Название; стоимость</code>. Попробуйте ещё раз или /cancel.")
        return

    def work(ctx):
        o = orders.get(ctx, data["order_id"])
        orders.add_item(ctx, o.id, kind=ItemKind.work, name=parts[0], unit=unit, unit_cost=cost, quantity=quantity,
                        section=o.title or "Изделие")
        return o.id
    oid = await call(message, work)
    if oid:
        await state.clear()
        await show_card(message, oid)


@router.callback_query(F.data.startswith("oi:rmlist:"))
async def remove_list(cq: CallbackQuery):
    oid = int(cq.data.split(":")[2])
    items = await call(cq, lambda ctx: [(i.id, f"🗑 {i.name} · {money(i.price, '')}"[:60])
                                        for i in orders.get(ctx, oid).items])
    if items is not None:
        await answer(cq, "Какую позицию удалить?",
                     kb(*[[(t, f"oi:rm:{oid}:{iid}")] for iid, t in items], [("🔙 Заказ", f"o:open:{oid}")]), edit=True)


@router.callback_query(F.data.startswith("oi:rm:"))
async def remove_item(cq: CallbackQuery):
    _, _, oid, iid = cq.data.split(":")
    if await call(cq, lambda ctx: orders.remove_item(ctx, int(oid), int(iid)).id):
        await show_card(cq, int(oid), edit=True)


# ---------------------------------------------------------------- наценки заказа

@router.callback_query(F.data.startswith("o:mk:"))
async def markups(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Item.markups)
    await state.update_data(order_id=int(cq.data.split(":")[2]))
    await answer(cq, "Наценки этого заказа, через пробел: <code>материалы фурнитура работы [фикс. сумма]</code>\n"
                     "Пример: <code>35 25 15 200000</code>")


@router.message(Item.markups, F.text)
async def markups_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    nums = message.text.replace(";", " ").split()
    if len(nums) < 3:
        await message.answer("Нужно минимум три числа: материалы, фурнитура, работы.")
        return
    try:
        vals = [parse_num(x) for x in nums[:4]]
    except Exception:
        await message.answer("Введите числа, например <code>35 25 15</code>.")
        return
    oid = await call(message, lambda ctx: orders.set_markups(
        ctx, data["order_id"], materials=vals[0], hardware=vals[1], works=vals[2],
        fixed=vals[3] if len(vals) > 3 else None).id)
    if oid:
        await state.clear()
        await show_card(message, oid)


# ---------------------------------------------------------------- PDF

@router.callback_query(F.data.startswith("o:pdf:"))
async def make_pdf(cq: CallbackQuery):
    oid = int(cq.data.split(":")[2])
    await cq.answer("Формирую смету…")

    def work(ctx):
        est = estimates.generate(ctx, oid)
        sub = tariffs.current_subscription(ctx.db, ctx.eid)
        left = sub.plan.estimates_limit - sub.estimates_used
        _, data = estimates.get_file(ctx, est.uid)
        return data, est.uid, money(est.total), left
    res = await call(cq, work)
    if res:
        data, uid, total, left = res
        await cq.message.answer_document(
            BufferedInputFile(data, filename=f"{uid}.pdf"),
            caption=f"📄 Смета {uid}\nИтого: <b>{total}</b>\nОсталось генераций в периоде: {left}\n\n"
                    "Перешлите этот файл клиенту в Telegram.")


# ---------------------------------------------------------------- статусы

@router.callback_query(F.data.startswith("o:st:"))
async def status_menu(cq: CallbackQuery):
    oid = int(cq.data.split(":")[2])
    ok = await call(cq, lambda ctx: tariffs.require_feature(ctx.db, ctx.eid, "order_statuses") and True)
    if ok:
        rows = [[(t, f"o:set:{oid}:{s.value}")] for s, t in orders.STATUS_TITLES.items()]
        await answer(cq, "Новый статус заказа:", kb(*rows, [("🔙 Заказ", f"o:open:{oid}")]), edit=True)


@router.callback_query(F.data.startswith("o:set:"))
async def status_set(cq: CallbackQuery):
    _, _, oid, st = cq.data.split(":")
    if await call(cq, lambda ctx: orders.change_status(ctx, int(oid), OrderStatus(st)).id):
        await show_card(cq, int(oid), edit=True)


# ---------------------------------------------------------------- расход и отходы (PRO+)

def waste_text(ctx: Ctx, oid: int) -> tuple[str, list]:
    rep = waste.report(ctx, oid)
    lines = [f"♻️ <b>Расход по заказу №{rep['order'].number}</b>", "план → факт · отходы"]
    buttons = []
    for u, m in rep["rows"]:
        fact = f"{qty(m['actual'])}" if m["actual"] is not None else "—"
        wst = f"{qty(m['waste'])}" if m["waste"] is not None else "—"
        dev = f" ({'+' if m['deviation'] > 0 else ''}{qty(m['deviation'])}, {qty(m['deviation_pct'])}%)" \
            if m["deviation"] is not None and m["deviation_pct"] is not None else ""
        wp = f", отходы {qty(m['waste_pct'])}%" if m["waste_pct"] is not None else ""
        lines.append(f"• {e(u.name)}: {qty(m['planned'])} → {fact} {e(u.unit)}{dev}; отходы {wst}{wp}")
        buttons.append([(f"✏️ {u.name}"[:40], f"w:edit:{oid}:{u.id}")])
    if rep["rows"]:
        lines.append(f"\nПерерасход в деньгах: <b>{money(rep['overrun_cost'])}</b>")
        if rep["waste_pct"] is not None:
            lines.append(f"Отходы (по стоимости): {qty(rep['waste_pct'])}%")
    else:
        lines.append("\nПлан ещё не сформирован.")
    return "\n".join(lines), buttons


@router.callback_query(F.data.startswith("w:show:"))
async def waste_show(cq: CallbackQuery):
    oid = int(cq.data.split(":")[2])

    def work(ctx):
        waste.plan_from_order(ctx, oid)  # план из позиций заказа (недостающие строки)
        return waste_text(ctx, oid)
    res = await call(cq, work)
    if res:
        text, buttons = res
        await answer(cq, text, kb(*buttons, [("🔙 Заказ", f"o:open:{oid}")]), edit=True)


@router.callback_query(F.data.startswith("w:edit:"))
async def waste_edit(cq: CallbackQuery, state: FSMContext):
    _, _, oid, uid = cq.data.split(":")
    await state.set_state(Item.waste)
    await state.update_data(order_id=int(oid), usage_id=int(uid))
    await answer(cq, "Фактический расход и отходы через пробел: <code>12,5 1,8</code>")


@router.message(Item.waste, F.text)
async def waste_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    parts = message.text.replace(";", " ").split()
    try:
        actual = parse_num(parts[0])
        wst = parse_num(parts[1]) if len(parts) > 1 else None
    except Exception:
        await message.answer("Введите два числа: факт и отходы, например <code>12,5 1,8</code>.")
        return

    def work(ctx):
        waste.set_actual(ctx, data["usage_id"], actual=actual, waste=wst)
        return waste_text(ctx, data["order_id"])
    res = await call(message, work)
    if res:
        await state.clear()
        text, buttons = res
        await message.answer(text, reply_markup=kb(*buttons, [("🔙 Заказ", f"o:open:{data['order_id']}")]))
