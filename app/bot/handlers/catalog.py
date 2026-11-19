"""База материалов и производственные затраты."""
from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.core import BTN_EXPENSES, BTN_MATERIALS, answer, call, e, kb, parse_num
from app.core.fmt import money
from app.models import ItemKind
from app.services import expenses, materials, tariffs

router = Router(name="catalog")
PAGE = 10


class Mat(StatesGroup):
    category = State()
    name = State()
    price = State()
    edit_price = State()


class Exp(StatesGroup):
    amount = State()
    comment = State()
    category = State()


# ---------------------------------------------------------------- материалы

def _mat_summary(ctx):
    us = tariffs.usage(ctx.db, ctx.eid)
    limit = us.get("materials_limit")
    return us["materials"], limit, ctx.can("materials.edit")


@router.message(F.text == BTN_MATERIALS)
async def materials_menu(message: Message, state: FSMContext):
    await state.clear()
    res = await call(message, lambda ctx: (ctx.require("materials.view"), _mat_summary(ctx))[1])
    if res is None:
        return
    used, limit, can_edit = res
    rows = [[("🪵 Материалы", "m:list:material:0"), ("🔩 Фурнитура", "m:list:hardware:0")]]
    if can_edit:
        rows.append([("➕ Добавить материал", "m:add:material"), ("➕ Добавить фурнитуру", "m:add:hardware")])
    lim = f" из {limit}" if limit else ""
    await message.answer(f"📦 <b>База материалов</b>\nПозиций: {used}{lim}", reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("m:list:"))
async def materials_list(cq: CallbackQuery):
    _, _, kind, page = cq.data.split(":")
    page = int(page)
    items = await call(cq, lambda ctx: [(m.id, f"{m.category + ' · ' if m.category else ''}{m.name} — "
                                               f"{money(m.price, '')}/{m.unit}"[:60])
                                        for m in materials.list_materials(ctx, kind=ItemKind(kind))])
    if items is None:
        return
    chunk = items[page * PAGE:(page + 1) * PAGE]
    rows = [[(t, f"m:open:{mid}")] for mid, t in chunk]
    nav = []
    if page:
        nav.append(("◀️", f"m:list:{kind}:{page - 1}"))
    if len(items) > (page + 1) * PAGE:
        nav.append(("▶️", f"m:list:{kind}:{page + 1}"))
    title = ("🪵 Материалы" if kind == "material" else "🔩 Фурнитура") + f" ({len(items)})"
    await answer(cq, title if items else title + "\nПока пусто.", kb(*rows, nav, [("➕ Добавить", f"m:add:{kind}")]),
                 edit=True)


@router.callback_query(F.data.startswith("m:open:"))
async def material_open(cq: CallbackQuery):
    mid = int(cq.data.split(":")[2])

    def work(ctx):
        m = materials.get(ctx, mid)
        return (f"<b>{e(m.name)}</b>\nКатегория: {e(m.category) or '—'}\nЕдиница: {e(m.unit)}\n"
                f"Цена: <b>{money(m.price)}</b> за {e(m.unit)}"), ctx.can("materials.edit"), m.kind.value
    res = await call(cq, work)
    if res:
        text, can_edit, kind = res
        rows = [[("✏️ Изменить цену", f"m:price:{mid}"), ("🗑 Удалить", f"m:del:{mid}")]] if can_edit else []
        await answer(cq, text, kb(*rows, [("🔙 Список", f"m:list:{kind}:0")]), edit=True)


@router.callback_query(F.data.startswith("m:add:"))
async def material_add(cq: CallbackQuery, state: FSMContext):
    kind = cq.data.split(":")[2]
    ok = await call(cq, lambda ctx: (ctx.require("materials.edit"), tariffs.require_active(ctx.db, ctx.eid), True)[2])
    if not ok:
        return
    await state.set_state(Mat.category)
    await state.update_data(kind=kind)
    cats = await call(cq, lambda ctx: materials.categories(ctx, ItemKind(kind))) or []
    hint = ("\nУже есть: " + ", ".join(e(c) for c in cats[:12])) if cats else ""
    example = "ЛДСП, МДФ, Столешница" if kind == "material" else "Петли, Направляющие, Ручки"
    await answer(cq, f"Категория (например: {example}). Или «-».{hint}")


@router.message(Mat.category, F.text)
async def mat_category(message: Message, state: FSMContext):
    await state.update_data(category="" if message.text.strip() == "-" else message.text.strip()[:80])
    await state.set_state(Mat.name)
    await message.answer("Название позиции (например: ЛДСП Egger 16 мм белый):")


@router.message(Mat.name, F.text)
async def mat_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:200])
    data = await state.get_data()
    units = materials.UNITS if data["kind"] == "material" else ["шт", "компл", "уп", "м.п."]
    await message.answer("Единица измерения:", reply_markup=kb(*[[(u, f"m:unit:{u}") for u in units[i:i + 3]]
                                                                for i in range(0, len(units), 3)]))


@router.callback_query(F.data.startswith("m:unit:"))
async def mat_unit(cq: CallbackQuery, state: FSMContext):
    await state.update_data(unit=cq.data.split(":", 2)[2])
    await state.set_state(Mat.price)
    await answer(cq, "Цена за единицу, сум:")


@router.message(Mat.price, F.text)
async def mat_price(message: Message, state: FSMContext):
    data = await state.get_data()
    if "unit" not in data:
        await message.answer("Сначала выберите единицу измерения кнопкой выше.")
        return
    try:
        price = parse_num(message.text)
    except Exception:
        await message.answer("Введите цену числом, например 95000.")
        return
    res = await call(message, lambda ctx: materials.create(ctx, kind=ItemKind(data["kind"]), name=data["name"],
                                                           unit=data["unit"], price=price,
                                                           category=data["category"]).id)
    await state.clear()
    if res:
        await message.answer(f"✅ Добавлено: {e(data['name'])} — {money(price)}/{e(data['unit'])}",
                             reply_markup=kb([("➕ Ещё", f"m:add:{data['kind']}"),
                                              ("📦 К списку", f"m:list:{data['kind']}:0")]))


@router.callback_query(F.data.startswith("m:price:"))
async def mat_edit_price(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Mat.edit_price)
    await state.update_data(material_id=int(cq.data.split(":")[2]))
    await answer(cq, "Новая цена за единицу, сум:")


@router.message(Mat.edit_price, F.text)
async def mat_price_saved(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        price = parse_num(message.text)
    except Exception:
        await message.answer("Введите цену числом.")
        return
    if await call(message, lambda ctx: materials.update(ctx, data["material_id"], price=price).id):
        await state.clear()
        await message.answer(f"✅ Цена обновлена: {money(price)}. Новые расчёты будут использовать новую цену.")


@router.callback_query(F.data.startswith("m:del:"))
async def mat_delete(cq: CallbackQuery):
    res = await call(cq, lambda ctx: materials.delete(ctx, int(cq.data.split(":")[2])))
    if res:
        await answer(cq, "🗑 Удалено." if res == "deleted" else
                     "📦 Позиция используется в заказах, поэтому перенесена в архив: старые расчёты сохранены, "
                     "в новых она не появится.", edit=True)


# ---------------------------------------------------------------- производственные затраты

@router.message(F.text == BTN_EXPENSES)
async def expenses_menu(message: Message, state: FSMContext):
    await state.clear()

    def work(ctx):
        today = date.today()
        items = expenses.list_expenses(ctx, date_from=today.replace(day=1), date_to=today)
        total = sum(x.amount for x in items)
        lines = [f"💸 <b>Производственные затраты</b> — {today:%m.%Y}", f"Итого за месяц: <b>{money(total)}</b>"]
        for x in items[:10]:
            lines.append(f"• {x.spent_on:%d.%m} {e(x.category.name)} — {money(x.amount)}"
                         + (f" ({e(x.comment)})" if x.comment else ""))
        return "\n".join(lines), ctx.can("expenses.edit")
    res = await call(message, work)
    if res:
        text, can_edit = res
        rows = [[("➕ Добавить затрату", "x:add"), ("🏷 Своя категория", "x:cat")]] if can_edit else []
        await message.answer(text + "\n\n<i>Сборка и монтаж сюда не входят — это работы цеха в расчёте заказа.</i>",
                             reply_markup=kb(*rows))


@router.callback_query(F.data == "x:add")
async def expense_add(cq: CallbackQuery):
    cats = await call(cq, lambda ctx: (tariffs.require_feature(ctx.db, ctx.eid, "expenses"),
                                       [(c.id, c.name) for c in expenses.categories(ctx)])[1])
    if cats:
        await answer(cq, "Категория затрат:", kb(*[[(n, f"x:c:{cid}")] for cid, n in cats]), edit=True)


@router.callback_query(F.data.startswith("x:c:"))
async def expense_cat(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Exp.amount)
    await state.update_data(category_id=int(cq.data.split(":")[2]))
    await answer(cq, "Сумма, сум:")


@router.message(Exp.amount, F.text)
async def expense_amount(message: Message, state: FSMContext):
    try:
        amount = parse_num(message.text)
    except Exception:
        await message.answer("Введите сумму числом.")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(Exp.comment)
    await message.answer("Комментарий (или «-»). Чтобы привязать к заказу, напишите его номер: <code>№12 распил фасадов</code>")


@router.message(Exp.comment, F.text)
async def expense_comment(message: Message, state: FSMContext):
    import re
    data = await state.get_data()
    text = message.text.strip()
    m = re.match(r"^№\s*(\d+)\s*(.*)$", text)
    number = int(m.group(1)) if m else None
    comment = (m.group(2) if m else text)
    comment = None if comment.strip() in ("", "-") else comment

    def work(ctx):
        from app.models import Order
        order_id = None
        if number:
            o = ctx.db.query(Order).filter_by(enterprise_id=ctx.eid, number=number).first()
            order_id = o.id if o else None
        expenses.add(ctx, category_id=data["category_id"], amount=data["amount"], comment=comment, order_id=order_id)
        return {"order_linked": bool(order_id)}
    res = await call(message, work)
    await state.clear()
    if res is not None:
        linked = f" и привязана к заказу №{number}" if res["order_linked"] else (
            f" (заказ №{number} не найден — без привязки)" if number else "")
        await message.answer(f"✅ Затрата {money(parse_num(data['amount']))} сохранена{linked}.")


@router.callback_query(F.data == "x:cat")
async def expense_new_cat(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Exp.category)
    await answer(cq, "Название новой категории затрат:")


@router.message(Exp.category, F.text)
async def expense_cat_saved(message: Message, state: FSMContext):
    name = message.text.strip()
    if await call(message, lambda ctx: expenses.add_category(ctx, name).id):
        await state.clear()
        await message.answer(f"✅ Категория «{e(name)}» добавлена.")
