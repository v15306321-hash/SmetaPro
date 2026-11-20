"""«🧮 Новый расчёт» и карточка заказа — поэтапно, только кнопками мини-меню.

Сценарий: имя клиента → телефон → сколько изделий → формат изделия №1 → карточка заказа.
Из карточки: материалы и фурнитура (stock.py), наценки, работа цеха, рабочие, PDF-смета,
удаление позиций, следующее изделие, статусы и расход (PRO).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message

from app.bot.core import (BACK, BTN_NEW, BTN_ORDERS, HOME, MANUAL, OTHER_NUMBER, SKIP, TO_ORDER, TO_ORDER_LONG,
                          amount_text, call, e, grid, main_menu, numbers_kb, price_buttons, read_money, read_qty, rk)
from app.core.fmt import money, qty
from app.models import ItemKind, OrderStatus
from app.services import estimates, orders, tariffs, waste
from app.services.accounts import Ctx

router = Router(name="calc")

FURNITURE = ["Кухонный гарнитур", "Шкаф-купе", "Диван", "Стол", "Журнальный столик", "Кровать", "Гардеробная",
             "Тумбочка"]
OTHER_PRODUCT = "Другое изделие📝"

C_MAT = "📦Материалы"
C_HW = "🔩Фурнитура"
C_MARKUP = "💵Настроить наценку"
C_WORK = "🛠Работа цеха"
C_WORKERS = "👥Добавить рабочих"
C_PDF = "📄PDF-смета"
C_REMOVE = "🗑Удалить позицию"
C_STATUS = "🔄Статус"
C_WASTE = "♻️Расход и отходы"
C_SWITCH = "🛋Сменить изделие"
C_NEXT_PREFIX = "🛋Изделие №"
MORE_ORDERS = "🔎 Ещё заказы"

WORKS = ["Распил", "Кромка", "Присадка", "Фрезеровка", "Покраска", "Шлифовка", "Сборка", "Монтаж", "Доставка",
         "Транспортировка"]
OTHER_WORK = "Другая работа📝"
PCT = [["0", "10", "20", "30"], ["40", "50", "70", "100"]]


class Calc(StatesGroup):
    client = State()
    phone = State()
    count = State()
    product = State()
    card = State()
    mk_work = State()
    work_name = State()
    work_price = State()
    workers_count = State()
    remove = State()
    status = State()
    switch = State()
    orders = State()
    waste_pick = State()
    waste_actual = State()
    waste_waste = State()


# ================================================================ карточка заказа

def _names(items) -> str:
    return ", ".join(f"{it.name} ({amount_text(it.quantity, it.unit)})" for it in items) + "."


def card_data(ctx: Ctx, order_id: int, product: str | None) -> tuple[str, dict]:
    o = orders.get(ctx, order_id)
    products = orders.products_of(o)
    current = product if product in products else (products[-1] if products else "")
    planned = max(o.products_planned or 1, len(products))
    lines = [f"📋 Заказ №{o.number}" + (f" — {e(o.title)}" if o.title else "")]
    client = ", ".join(x for x in [o.client_name, o.client_phone] if x)
    if client:
        lines.append(f"👤 {e(client)}")
    lines.append(f"Статус: {orders.STATUS_TITLES[o.status]}")
    if planned > 1 and current:
        idx = products.index(current) + 1 if current in products else len(products)
        lines.append(f"🛋 Сейчас заполняем: {e(current)} (изделие {idx} из {planned})")
    lines.append("")
    if not o.items:
        lines.append("Позиций пока нет — добавьте материалы, фурнитуру и работы.")
    else:
        sections = orders.grouped_items(o)
        for section, items in sections:
            if len(sections) > 1:
                lines.append(f"🛋 <b>{e(section)}</b>")
            for kind, label in ((ItemKind.material, "📦 Материалы"), (ItemKind.hardware, "🔩 Фурнитура"),
                                (ItemKind.work, "🛠 Тех. работы")):
                group = [it for it in items if it.kind == kind]
                if group:
                    lines.append(f"{label}: {e(_names(group))}")
            if len(sections) > 1:
                lines.append("")
    if o.workers:
        lines.append("👥 Рабочие: " + "; ".join(
            f"{e(w.name)} ({e(w.position)}) — {money(w.amount)}" for w in o.workers) + ".")
    if lines[-1] != "":
        lines.append("")
    lines += [f"Себестоимость: {money(o.cost_total)}",
              f"Стоимость работы цеха ({qty(o.markup_works)}%): {money(o.workshop_fee)}"]
    if o.fixed_addition:
        lines.append(f"Доплата: {money(o.fixed_addition)}")
    lines.append(f"💰 <b>Итого клиенту: {money(o.price_total)}</b>")
    if o.workers:
        lines += ["", f"Оплата рабочим: {money(o.workers_cost)}", f"Прибыль цеха: {money(o.profit)}"]
    info = dict(id=o.id, product=current, products=products, planned=planned,
                has_items=bool(o.items) or bool(o.workers),
                can_edit=ctx.can("orders.edit"), can_pdf=ctx.can("estimates.create"),
                statuses=tariffs.has_feature(ctx.db, ctx.eid, "order_statuses") and ctx.can("orders.status"),
                waste=tariffs.has_feature(ctx.db, ctx.eid, "waste") and ctx.can("waste.edit"))
    return "\n".join(lines).strip(), info


def card_kb(info: dict):
    rows = []
    if info["can_edit"]:
        rows += [[C_MAT, C_HW], [C_MARKUP, C_WORK]]
        nav = [C_WORKERS]
        if len(info["products"]) < info["planned"]:
            nav.append(f"{C_NEXT_PREFIX}{len(info['products']) + 1}")
        elif len(info["products"]) > 1:
            nav.append(C_SWITCH)
        rows.append(nav)
    if info["has_items"]:
        rows.append([b for b, ok in ((C_PDF, info["can_pdf"]), (C_REMOVE, info["can_edit"])) if ok])
        rows.append([b for b, ok in ((C_STATUS, info["statuses"]), (C_WASTE, info["waste"])) if ok])
    rows.append([HOME, BACK])
    return rk(*rows, placeholder="Выберите действие")


async def show_card(message: Message, state: FSMContext, prefix: str = ""):
    data = await state.get_data()
    oid = data.get("order_id")
    if not oid:
        await message.answer("Заказ не выбран.", reply_markup=main_menu())
        await state.clear()
        return
    res = await call(message, lambda ctx: card_data(ctx, oid, data.get("product")))
    if not res:
        return
    text, info = res
    await state.set_state(Calc.card)
    await state.update_data(product=info["product"])
    await message.answer((prefix + "\n\n" if prefix else "") + text, reply_markup=card_kb(info))


@router.message(F.text.in_({TO_ORDER, TO_ORDER_LONG}))
async def back_to_order(message: Message, state: FSMContext):
    """«🔙Заказ» работает с любого шага: незавершённое действие отменяется, заказ сохранён."""
    if (await state.get_data()).get("order_id"):
        await show_card(message, state)
    else:
        await state.clear()
        await message.answer("🏛️ Главное меню", reply_markup=main_menu())


# ================================================================ новый расчёт

@router.message(F.text == BTN_NEW)
async def new_calc(message: Message, state: FSMContext):
    await state.clear()
    ok = await call(message, lambda ctx: (ctx.require("orders.edit"), tariffs.require_active(ctx.db, ctx.eid), 1)[2],
                    keyboard=main_menu())
    if not ok:
        return
    await state.set_state(Calc.client)
    await message.answer("📉Новый расчет открыт!\n\nУкажите имя клиента? (Или пропустить)", reply_markup=rk([SKIP], [HOME]))


@router.message(Calc.client, F.text)
async def calc_client(message: Message, state: FSMContext):
    await state.update_data(client="" if message.text == SKIP else message.text.strip()[:200])
    await state.set_state(Calc.phone)
    await message.answer("✅Отлично!\n\n📞Укажите номер телефона клиента? (Или пропустить)",
                         reply_markup=rk([SKIP], [BACK, HOME]))


@router.message(Calc.phone, F.text == BACK)
async def calc_phone_back(message: Message, state: FSMContext):
    await state.set_state(Calc.client)
    await message.answer("Укажите имя клиента? (Или пропустить)", reply_markup=rk([SKIP], [HOME]))


@router.message(Calc.phone, F.text)
async def calc_phone(message: Message, state: FSMContext):
    await state.update_data(phone=None if message.text == SKIP else message.text.strip()[:32])
    await state.set_state(Calc.count)
    await message.answer("✅Отлично!\n\nУкажите какое количество мебели будет в этом заказе:",
                         reply_markup=numbers_kb([BACK, HOME]))


@router.message(Calc.count, F.text == BACK)
async def calc_count_back(message: Message, state: FSMContext):
    await state.set_state(Calc.phone)
    await message.answer("📞Укажите номер телефона клиента? (Или пропустить)", reply_markup=rk([SKIP], [BACK, HOME]))


@router.message(Calc.count, F.text == OTHER_NUMBER)
async def calc_count_other(message: Message):
    await message.answer("✍ Напишите количество изделий цифрой, например 12:")


@router.message(Calc.count, F.text)
async def calc_count(message: Message, state: FSMContext):
    n = read_qty(message.text)
    if not n or n != int(n) or not 1 <= n <= 100:
        await message.answer("Напишите целое число от 1 до 100 🙂")
        return
    await state.update_data(planned=int(n), product_idx=0)
    await ask_product(message, state, 0)


async def ask_product(message: Message, state: FSMContext, idx: int):
    await state.set_state(Calc.product)
    await state.update_data(product_idx=idx)
    data = await state.get_data()
    tail = [TO_ORDER, HOME] if data.get("order_id") else [BACK, HOME]
    await message.answer(("✅Отлично!\n\n" if idx == 0 else "") +
                         f"🛋Пожалуйста напишите мебель какого формата в №{idx + 1}?",
                         reply_markup=rk(*grid(FURNITURE, 2), [OTHER_PRODUCT], tail))


@router.message(Calc.product, F.text == BACK)
async def calc_product_back(message: Message, state: FSMContext):
    await state.set_state(Calc.count)
    await message.answer("Укажите какое количество мебели будет в этом заказе:", reply_markup=numbers_kb([BACK, HOME]))


@router.message(Calc.product, F.text == OTHER_PRODUCT)
async def calc_product_other(message: Message):
    await message.answer("✍ Напишите название изделия, например: Детская стенка")


@router.message(Calc.product, F.text)
async def calc_product(message: Message, state: FSMContext):
    name = message.text.strip()[:120]
    if len(name) < 2:
        await message.answer("Название слишком короткое — напишите, что изготавливаем 🙂")
        return
    data = await state.get_data()
    idx = data.get("product_idx", 0)

    if data.get("order_id"):
        oid = await call(message, lambda ctx: orders.set_product(ctx, data["order_id"], idx, name).id)
    else:
        oid = await call(message, lambda ctx: orders.create(
            ctx, title=name, client_name=data.get("client", ""), client_phone=data.get("phone"),
            products_planned=data.get("planned", 1)).id, keyboard=main_menu())
    if oid:
        await state.update_data(order_id=oid, product=name)
        await show_card(message, state)


# ================================================================ действия карточки

@router.message(Calc.card, F.text.in_({C_MAT, C_HW}))
async def card_stock(message: Message, state: FSMContext):
    from app.bot.handlers.stock import start_pick
    await start_pick(message, state, ItemKind.material if message.text == C_MAT else ItemKind.hardware)


@router.message(Calc.card, F.text.startswith(C_NEXT_PREFIX))
async def card_next_product(message: Message, state: FSMContext):
    data = await state.get_data()
    res = await call(message, lambda ctx: len(orders.products_of(orders.get(ctx, data["order_id"]))))
    if res is not None:
        await ask_product(message, state, res)


@router.message(Calc.card, F.text == C_SWITCH)
async def card_switch(message: Message, state: FSMContext):
    data = await state.get_data()
    products = await call(message, lambda ctx: orders.products_of(orders.get(ctx, data["order_id"])))
    if products:
        await state.set_state(Calc.switch)
        await message.answer("🛋 С каким изделием работаем?", reply_markup=rk(*grid(products, 2), [BACK]))


@router.message(Calc.switch, F.text)
async def card_switch_pick(message: Message, state: FSMContext):
    if message.text != BACK:
        await state.update_data(product=message.text.strip())
    await show_card(message, state)


@router.message(Calc.card, F.text == BACK)
async def card_back(message: Message, state: FSMContext):
    await orders_list(message, state)


# ---------------------------------------------------------------- наценка (стоимость работы цеха)

@router.message(Calc.card, F.text == C_MARKUP)
async def mk_start(message: Message, state: FSMContext):
    data = await state.get_data()
    cur = await call(message, lambda ctx: orders.get(ctx, data["order_id"]).markup_works)
    if cur is not None:
        await state.set_state(Calc.mk_work)
        await message.answer(
            "💵 Стоимость работы цеха — ваша наценка на общую сумму материалов, фурнитуры и технических работ.\n\n"
            f"Сейчас: {qty(cur)}%. Выберите или напишите свой процент:", reply_markup=rk(*PCT, [TO_ORDER]))


@router.message(Calc.mk_work, F.text)
async def mk_work(message: Message, state: FSMContext):
    v = 0 if message.text.strip() == "0" else read_qty(message.text)
    if v is None or not 0 <= v <= 1000:
        await message.answer("Напишите процент числом от 0 до 1000, например 25")
        return
    data = await state.get_data()
    if await call(message, lambda ctx: orders.set_markups(ctx, data["order_id"], works=v).id):
        await show_card(message, state, f"✅ Стоимость работы цеха: {qty(v)}%")


# ---------------------------------------------------------------- работа цеха

@router.message(Calc.card, F.text == C_WORK)
async def work_start(message: Message, state: FSMContext):
    await state.set_state(Calc.work_name)
    await message.answer("🛠 Какую техническую работу добавить? Наценка на неё отдельно не начисляется — "
                         "она входит в общую сумму.", reply_markup=rk(*grid(WORKS, 2), [OTHER_WORK], [TO_ORDER]))


@router.message(Calc.work_name, F.text == OTHER_WORK)
async def work_other(message: Message):
    await message.answer("✍ Напишите название работы, например: Установка подсветки")


@router.message(Calc.work_name, F.text)
async def work_name(message: Message, state: FSMContext):
    name = message.text.strip()[:120]
    await state.update_data(work_name=name)
    prices = await call(message, lambda ctx: orders.recent_work_prices(ctx)) or []
    await state.set_state(Calc.work_price)
    await message.answer(f"✅Отлично!\n\n💵 Укажите стоимость работы «{e(name)}»:",
                         reply_markup=rk(*grid(price_buttons(prices), 3), [MANUAL], [TO_ORDER]))


@router.message(Calc.work_price, F.text == MANUAL)
async def work_price_manual(message: Message):
    await message.answer("✍ Напишите стоимость цифрами, например 900.000")


@router.message(Calc.work_price, F.text)
async def work_price(message: Message, state: FSMContext):
    price = read_money(message.text)
    if price is None:
        await message.answer("Напишите стоимость цифрами, например 900.000")
        return
    data = await state.get_data()
    ok = await call(message, lambda ctx: orders.add_item(ctx, data["order_id"], kind=ItemKind.work,
                                                         name=data["work_name"], unit="услуга", unit_cost=price,
                                                         quantity=1, section=data.get("product") or "Изделие").id)
    if ok:
        await show_card(message, state, f"✅ Работа «{e(data['work_name'])}» добавлена — {money(price)}")


# ---------------------------------------------------------------- рабочие (сотрудники цеха)

NEW_EMPLOYEE = "➕ Новый сотрудник"


@router.message(Calc.card, F.text == C_WORKERS)
async def workers_start(message: Message, state: FSMContext):
    from app.services import employees

    def work(ctx):
        return ([(emp.id, f"{emp.name} · {emp.position} · {employees.pay_text(emp)}"[:60])
                 for emp in employees.list_active(ctx)], ctx.can("staff.manage"))
    res = await call(message, work)
    if not res:
        return
    staff_list, can_add = res
    await state.set_state(Calc.workers_count)
    await state.update_data(worker_map={t: eid for eid, t in staff_list})
    rows = [[t] for _, t in staff_list] + ([[NEW_EMPLOYEE]] if can_add else []) + [[TO_ORDER]]
    if staff_list:
        text = "👥 Кого добавить к заказу? Выберите сотрудника:"
    else:
        text = ("👥 У вас пока нет сотрудников.\n\nДобавьте сотрудника — укажете должность, имя и оплату, "
                "и он появится в этом списке." if can_add else "👥 Сотрудников пока нет. Их добавляет владелец.")
    await message.answer(text, reply_markup=rk(*rows))


@router.message(Calc.workers_count, F.text == NEW_EMPLOYEE)
async def workers_new(message: Message, state: FSMContext):
    from app.bot.handlers.business import start_add_employee
    await start_add_employee(message, state, origin="order")


@router.message(Calc.workers_count, F.text)
async def workers_pick(message: Message, state: FSMContext):
    eid = (await state.get_data()).get("worker_map", {}).get(message.text)
    if not eid:
        await message.answer("Выберите сотрудника кнопкой ниже 👇")
        return
    await assign_worker(message, state, eid)


async def assign_worker(message: Message, state: FSMContext, employee_id: int):
    data = await state.get_data()
    res = await call(message, lambda ctx: (lambda w: (w.name, str(w.amount)))(
        orders.add_worker(ctx, data["order_id"], employee_id)))
    if res:
        await show_card(message, state, f"✅ {e(res[0])} добавлен к заказу — {money(res[1])}")


# ---------------------------------------------------------------- PDF

@router.message(Calc.card, F.text == C_PDF)
async def make_pdf(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("⏳ Формирую смету…")

    def work(ctx):
        est = estimates.generate(ctx, data["order_id"])
        _, pdf = estimates.get_file(ctx, est.uid)
        sub = tariffs.current_subscription(ctx.db, ctx.eid)
        return pdf, est.uid, money(est.total), sub.plan.estimates_limit - sub.estimates_used
    res = await call(message, work)
    if res:
        pdf, uid, total, left = res
        await message.answer_document(
            BufferedInputFile(pdf, filename=f"{uid}.pdf"),
            caption=f"📄 Смета {uid}\n💰 Итого: <b>{total}</b>\n\nПерешлите этот файл клиенту.\n"
                    f"Осталось генераций смет в этом периоде: {left}")


# ---------------------------------------------------------------- удаление позиции

@router.message(Calc.card, F.text == C_REMOVE)
async def remove_start(message: Message, state: FSMContext):
    data = await state.get_data()
    def work(ctx):
        o = orders.get(ctx, data["order_id"])
        rows = [(["item", i.id], f"❌ {n}. {i.name} — {money(i.price, '').strip()}"[:60])
                for n, i in enumerate(o.items, 1)]
        rows += [(["worker", w.id], f"❌ 👥 {w.name} — {money(w.amount, '').strip()}"[:60]) for w in o.workers]
        return rows
    items = await call(message, work)
    if items:
        await state.set_state(Calc.remove)
        await state.update_data(remove_map={t: iid for iid, t in items})
        await message.answer("🗑 Какую позицию удалить?", reply_markup=rk(*[[t] for _, t in items], [BACK]))


@router.message(Calc.remove, F.text)
async def remove_pick(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("remove_map", {}).get(message.text)
    if message.text == BACK or not target:
        await show_card(message, state)
        return
    kind, tid = target
    fn = orders.remove_worker if kind == "worker" else orders.remove_item
    if await call(message, lambda ctx: fn(ctx, data["order_id"], tid).id):
        await show_card(message, state, "🗑 Удалено из заказа.")


# ---------------------------------------------------------------- статус (PRO)

STATUS_BY_TITLE = {t: s for s, t in orders.STATUS_TITLES.items()}


@router.message(Calc.card, F.text == C_STATUS)
async def status_start(message: Message, state: FSMContext):
    await state.set_state(Calc.status)
    await message.answer("🔄 Выберите новый статус заказа:",
                         reply_markup=rk(*grid(list(orders.STATUS_TITLES.values()), 2), [BACK]))


@router.message(Calc.status, F.text)
async def status_pick(message: Message, state: FSMContext):
    st = STATUS_BY_TITLE.get(message.text)
    if not st:
        await show_card(message, state)
        return
    data = await state.get_data()
    if await call(message, lambda ctx: orders.change_status(ctx, data["order_id"], OrderStatus(st)).id):
        await show_card(message, state, f"✅ Статус: {message.text}")


# ---------------------------------------------------------------- расход и отходы (PRO)

def _waste_screen(ctx: Ctx, oid: int):
    waste.plan_from_order(ctx, oid)
    rep = waste.report(ctx, oid)
    lines = [f"♻️ <b>Расход по заказу №{rep['order'].number}</b>", "план → факт · отходы", ""]
    buttons = {}
    for u, m in rep["rows"]:
        fact = qty(m["actual"]) if m["actual"] is not None else "—"
        wst = qty(m["waste"]) if m["waste"] is not None else "—"
        lines.append(f"• {e(u.name)}: {qty(m['planned'])} → {fact} {e(u.unit)}; отходы {wst}")
        buttons[f"✏️ {u.name}"[:60]] = (u.id, u.name, u.unit, str(m["planned"]))
    if rep["rows"]:
        lines.append(f"\nПерерасход в деньгах: <b>{money(rep['overrun_cost'])}</b>")
        if rep["waste_pct"] is not None:
            lines.append(f"Отходы: {qty(rep['waste_pct'])}% (по стоимости)")
    return "\n".join(lines), buttons


async def _show_waste(message: Message, state: FSMContext, prefix: str = ""):
    data = await state.get_data()
    res = await call(message, lambda ctx: _waste_screen(ctx, data["order_id"]))
    if res:
        text, buttons = res
        await state.set_state(Calc.waste_pick)
        await state.update_data(waste_map=buttons)
        await message.answer((prefix + "\n\n" if prefix else "") + text +
                             "\n\nВыберите позицию, чтобы указать фактический расход:",
                             reply_markup=rk(*[[b] for b in buttons], [BACK]))


@router.message(Calc.card, F.text == C_WASTE)
async def waste_start(message: Message, state: FSMContext):
    await _show_waste(message, state)


@router.message(Calc.waste_pick, F.text)
async def waste_pick(message: Message, state: FSMContext):
    row = (await state.get_data()).get("waste_map", {}).get(message.text)
    if not row:
        await show_card(message, state)
        return
    uid, name, unit, planned = row
    await state.update_data(waste_row=row)
    await state.set_state(Calc.waste_actual)
    await message.answer(f"📏 Сколько фактически ушло «{e(name)}»? ({unit})\n\nПо плану: {qty(planned)}",
                         reply_markup=rk([qty(planned)], [BACK]))


@router.message(Calc.waste_actual, F.text)
async def waste_actual(message: Message, state: FSMContext):
    if message.text == BACK:
        await _show_waste(message, state)
        return
    v = read_qty(message.text) if message.text.strip() != "0" else 0
    if v is None:
        await message.answer("Напишите число, например 3,5")
        return
    await state.update_data(waste_actual=str(v))
    await state.set_state(Calc.waste_waste)
    await message.answer("✅Отлично!\n\n♻️ Сколько из этого ушло в отходы? (Или пропустить)",
                         reply_markup=rk([SKIP, "0"], [BACK]))


@router.message(Calc.waste_waste, F.text)
async def waste_waste(message: Message, state: FSMContext):
    if message.text == BACK:
        await _show_waste(message, state)
        return
    w = None if message.text == SKIP else (read_qty(message.text) if message.text.strip() != "0" else 0)
    if message.text != SKIP and w is None:
        await message.answer("Напишите число, например 0,5")
        return
    data = await state.get_data()
    if await call(message, lambda ctx: waste.set_actual(ctx, data["waste_row"][0], actual=data["waste_actual"],
                                                        waste=w).id):
        await _show_waste(message, state, "✅ Сохранено!")


# ================================================================ список заказов

@router.message(F.text == BTN_ORDERS)
async def orders_menu(message: Message, state: FSMContext):
    await state.clear()
    await orders_list(message, state)


async def orders_list(message: Message, state: FSMContext, page: int = 0):
    per = 15

    def work(ctx):
        items = orders.list_orders(ctx, limit=per + 1, offset=page * per)
        return [(o.id, f"№{o.number} · {o.client_name or 'без имени'} · {o.title or '—'}"[:60]) for o in items]
    rows = await call(message, work, keyboard=main_menu())
    if rows is None:
        return
    if not rows and page == 0:
        await state.clear()
        await message.answer(f"📋 Заказов пока нет. Нажмите «{BTN_NEW}», чтобы сделать первый расчёт.",
                             reply_markup=main_menu())
        return
    keep = {k: v for k, v in (await state.get_data()).items() if k in ("order_id",)}
    await state.set_state(Calc.orders)
    await state.update_data(orders_map={t: oid for oid, t in rows[:per]}, orders_page=page, **keep)
    more = [MORE_ORDERS] if len(rows) > per else []
    await message.answer("📋 Ваши заказы (новые сверху). Выберите заказ:",
                         reply_markup=rk(*[[t] for _, t in rows[:per]], more, [HOME]))


@router.message(Calc.orders, F.text == MORE_ORDERS)
async def orders_more(message: Message, state: FSMContext):
    await orders_list(message, state, (await state.get_data()).get("orders_page", 0) + 1)


@router.message(Calc.orders, F.text)
async def orders_pick(message: Message, state: FSMContext):
    oid = (await state.get_data()).get("orders_map", {}).get(message.text)
    if not oid:
        await message.answer("Выберите заказ кнопкой ниже 👇")
        return
    await state.update_data(order_id=oid, product=None)
    await show_card(message, state)


@router.message(Calc.card)
async def card_unknown(message: Message):
    await message.answer("Выберите действие кнопкой ниже 👇")


_ = TO_ORDER_LONG
