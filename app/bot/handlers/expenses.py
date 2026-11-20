"""«💸 Затраты» — производственные затраты (BUSINESS, PRO, ENTERPRISE), поэтапно.
Сборка и монтаж сюда не входят — это работы цеха в расчёте заказа."""
from __future__ import annotations

from datetime import date

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from app.bot.core import BACK, BTN_EXPENSES, HOME, SKIP, call, e, grid, main_menu, read_money, rk
from app.core.fmt import money
from app.services import expenses, orders

router = Router(name="expenses")

X_ADD = "➕ Добавить затрату"
X_CAT = "🏷 Новая категория"


class Exp(StatesGroup):
    menu = State()
    category = State()
    amount = State()
    order = State()
    comment = State()
    new_category = State()


async def expenses_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx):
        today = date.today()
        items = expenses.list_expenses(ctx, date_from=today.replace(day=1), date_to=today)
        total = sum(x.amount for x in items)
        lines = [f"💸 <b>Производственные затраты</b> — {today:%m.%Y}", f"Итого за месяц: <b>{money(total)}</b>"]
        if items:
            lines.append("")
        for x in items[:10]:
            lines.append(f"• {x.spent_on:%d.%m} {e(x.category.name)} — {money(x.amount)}"
                         + (f" ({e(x.comment)})" if x.comment else ""))
        return "\n".join(lines), ctx.can("expenses.edit")
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    text, can_edit = res
    await state.set_state(Exp.menu)
    rows = [[X_ADD], [X_CAT]] if can_edit else []
    await message.answer((prefix + "\n\n" if prefix else "") + text +
                         "\n\n<i>Сборка и монтаж сюда не входят — это работа цеха в расчёте заказа.</i>",
                         reply_markup=rk(*rows, [HOME]))


@router.message(F.text == BTN_EXPENSES)
async def expenses_entry(message: Message, state: FSMContext):
    await expenses_menu(message, state)


@router.message(Exp.menu, F.text == X_ADD)
async def add_start(message: Message, state: FSMContext):
    cats = await call(message, lambda ctx: [(c.id, c.name) for c in expenses.categories(ctx)])
    if cats:
        await state.set_state(Exp.category)
        await state.update_data(cat_map={n: cid for cid, n in cats})
        await message.answer("🏷 Выберите категорию затраты:", reply_markup=rk(*grid([n for _, n in cats], 2), [BACK]))


@router.message(Exp.category, F.text)
async def add_category_pick(message: Message, state: FSMContext):
    if message.text == BACK:
        await expenses_menu(message, state)
        return
    cid = (await state.get_data()).get("cat_map", {}).get(message.text)
    if not cid:
        await message.answer("Выберите категорию кнопкой ниже 👇")
        return
    await state.update_data(category_id=cid, category_name=message.text)
    await state.set_state(Exp.amount)
    await message.answer(f"✅Отлично!\n\n💵 Сумма затраты «{e(message.text)}», сум:", reply_markup=rk([BACK]))


@router.message(Exp.amount, F.text)
async def add_amount(message: Message, state: FSMContext):
    if message.text == BACK:
        await add_start(message, state)
        return
    amount = read_money(message.text)
    if not amount:
        await message.answer("Напишите сумму цифрами, например 150.000")
        return
    await state.update_data(amount=str(amount))
    recent = await call(message, lambda ctx: [(o.id, f"№{o.number} · {o.client_name or o.title or ''}"[:40])
                                              for o in orders.list_orders(ctx, limit=8)]) or []
    await state.set_state(Exp.order)
    await state.update_data(order_map={t: oid for oid, t in recent})
    await message.answer("✅Отлично!\n\n📋 К какому заказу относится затрата? (Или пропустить)",
                         reply_markup=rk([SKIP], *grid([t for _, t in recent], 2)))


@router.message(Exp.order, F.text)
async def add_order(message: Message, state: FSMContext):
    oid = None if message.text == SKIP else (await state.get_data()).get("order_map", {}).get(message.text)
    if message.text != SKIP and not oid:
        await message.answer("Выберите заказ кнопкой или нажмите «⏩Пропустить»")
        return
    await state.update_data(exp_order=oid)
    await state.set_state(Exp.comment)
    await message.answer("✅Отлично!\n\n💬 Комментарий к затрате? (Или пропустить)", reply_markup=rk([SKIP]))


@router.message(Exp.comment, F.text)
async def add_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    comment = None if message.text == SKIP else message.text.strip()
    ok = await call(message, lambda ctx: expenses.add(ctx, category_id=data["category_id"], amount=data["amount"],
                                                      comment=comment, order_id=data.get("exp_order")).id)
    if ok:
        await expenses_menu(message, state, f"✅ Затрата «{e(data['category_name'])}» "
                                            f"{money(read_money(data['amount']))} сохранена!")


@router.message(Exp.menu, F.text == X_CAT)
async def new_cat(message: Message, state: FSMContext):
    await state.set_state(Exp.new_category)
    await message.answer("✍ Напишите название новой категории затрат, например: Упаковка", reply_markup=rk([BACK]))


@router.message(Exp.new_category, F.text)
async def new_cat_saved(message: Message, state: FSMContext):
    if message.text == BACK:
        await expenses_menu(message, state)
        return
    name = message.text.strip()
    if await call(message, lambda ctx: expenses.add_category(ctx, name).id):
        await expenses_menu(message, state, f"✅ Категория «{e(name)}» добавлена.")
