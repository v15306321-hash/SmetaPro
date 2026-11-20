"""Материалы и фурнитура: выбор из базы в заказ, поэтапное добавление новой позиции,
заказ услуги настройки базы, раздел «📦 Материалы» главного меню.

Добавление позиции (по одному вопросу):
  название → тип (корпусное покрытие / фурнитура / ткань / краска) → размер листа или единица →
  цена (кнопки с ранее введёнными ценами) → [в заказе] количество → карточка «МАТЕРИАЛ №N».
"""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from app.bot.core import (BACK, BTN_MATERIALS, BTN_SUB, HOME, MANUAL, NO, OTHER_NUMBER, TO_ORDER, TO_ORDER_LONG, YES,
                          UNIT_FORMS, amount_text, call, e, grid, main_menu, numbers_kb, plural, price_buttons,
                          read_money, read_qty, rk)
from app.config import get_settings
from app.core.fmt import money
from app.models import ItemKind, User
from app.services import materials, orders, tariffs
from app.services.accounts import Ctx
from app.services.audit import log

router = Router(name="stock")

T_BODY = "📦Корпусное покрытие"
T_HW = "🔩Фурнитура"
T_FABRIC = "🧺Ткань"
T_PAINT = "🫟Краска"
# тип → (вид позиции, категория в базе, единица по умолчанию: None — спросить)
TYPES = {
    T_BODY: (ItemKind.material, "Корпусное покрытие", "лист"),
    T_HW: (ItemKind.hardware, "Фурнитура", None),
    T_FABRIC: (ItemKind.material, "Ткань", "м.п."),
    T_PAINT: (ItemKind.material, "Краска", None),
}
UNITS_FOR = {"Фурнитура": ["шт", "компл", "пара", "уп"], "Краска": ["л", "кг"]}
STD_SHEETS = [(1220, 2440), (1830, 2750), (2070, 2800), (2800, 2070)]

E_SETUP = "📝Настроить"
E_BUY = "✅Приобрести настройку"
NEW_ITEM = "📝Добавить новый"
ADD_MORE = "📝Добавить ещё"
SETUP_DONE = "✅ Готово, всё отправил"
TO_STOCK = "↩️К материалам"
M_MATS = "📦 Материалы в базе"
M_HW = "🔩 Фурнитура в базе"
M_ADD = "📝 Добавить позицию"
M_BUY = "✅ Приобрести настройку"
I_PRICE = "✏️ Изменить цену"
I_DELETE = "🗑 Удалить"
I_BACK = "↩️К списку"


class Stock(StatesGroup):
    pick = State()
    empty = State()
    name = State()
    mtype = State()
    size = State()
    unit = State()
    price = State()
    qty = State()
    added = State()
    setup = State()
    menu = State()
    list = State()
    item = State()
    item_price = State()
    item_delete = State()


def _is_order(data: dict) -> bool:
    return data.get("origin") == "order" and bool(data.get("order_id"))


def _exit_btn(data: dict) -> str:
    return TO_ORDER if _is_order(data) else TO_STOCK


def _word(kind: str) -> str:
    return "фурнитуру" if kind == ItemKind.hardware.value else "материал"


def _label(m) -> str:
    return f"{m.name} · {money(m.price, '').strip()}/{UNIT_FORMS.get(m.unit, (m.unit,))[0]}"[:60]


# ================================================================ выбор из базы для заказа

async def start_pick(message: Message, state: FSMContext, kind: ItemKind):
    """Из карточки заказа: «📦Материалы» / «🔩Фурнитура»."""
    await state.update_data(origin="order", kind=kind.value)
    items = await call(message, lambda ctx: [(m.id, _label(m)) for m in materials.list_materials(ctx, kind=kind)])
    if items is None:
        return
    if not items:
        what = "материалов" if kind == ItemKind.material else "фурнитуры"
        await state.set_state(Stock.empty)
        await message.answer(
            f"Уважаемый пользователь!\n\nВ вашей базе пока нет {what}. Вы можете заказать услугу настройки "
            f"за {money(get_settings().setup_service_price)} — наш специалист сам заполнит базу, — "
            "или добавить позиции самостоятельно: нажмите «📝Настроить».",
            reply_markup=rk([E_SETUP], [E_BUY], [TO_ORDER]))
        return
    await _show_pick(message, state, items, kind)


async def _show_pick(message: Message, state: FSMContext, items: list, kind: ItemKind, note: str = ""):
    shown = items[:40]
    await state.set_state(Stock.pick)
    await state.update_data(pick_map={t: mid for mid, t in shown})
    title = "📦 Выберите материал из вашей базы" if kind == ItemKind.material else "🔩 Выберите фурнитуру из базы"
    hint = "\n\n🔎 Можно написать часть названия для поиска." if len(items) > 10 else ""
    await message.answer(f"{note}{title} или добавьте новый:{hint}",
                         reply_markup=rk(*[[t] for _, t in shown], [NEW_ITEM], [TO_ORDER]))


@router.message(Stock.pick, F.text == NEW_ITEM)
@router.message(Stock.empty, F.text == E_SETUP)
async def pick_new(message: Message, state: FSMContext):
    await start_new(message, state, ItemKind((await state.get_data())["kind"]), origin="order")


@router.message(Stock.empty, F.text == E_BUY)
async def pick_buy(message: Message, state: FSMContext):
    await start_setup(message, state)


@router.message(Stock.pick, F.text)
async def pick_item(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data.get("pick_map", {}).get(message.text)
    kind = ItemKind(data["kind"])
    if not mid:  # поиск по части названия
        needle = message.text.strip().lower()
        items = await call(message, lambda ctx: [(m.id, _label(m)) for m in materials.list_materials(ctx, kind=kind)
                                                 if needle in m.name.lower()])
        if items is None:
            return
        if len(items) == 1:
            mid = items[0][0]
        else:
            await _show_pick(message, state, items, kind, "Ничего не нашлось. " if not items else "🔎 Найдено:\n\n")
            return
    info = await call(message, lambda ctx: (lambda m: dict(id=m.id, name=m.name, unit=m.unit, price=str(m.price),
                                                           attrs=m.attrs or {}))(materials.get(ctx, mid)))
    if info:
        await state.update_data(material_id=info["id"], new_name=info["name"], unit=info["unit"],
                                price=info["price"], created=False,
                                sheet=[info["attrs"].get("sheet_w"), info["attrs"].get("sheet_h")]
                                if info["attrs"].get("sheet_w") else None)
        await ask_qty(message, state)


# ================================================================ новая позиция

async def start_new(message: Message, state: FSMContext, kind: ItemKind | None, origin: str):
    await state.update_data(origin=origin, kind=kind.value if kind else None, sheet=None, unit=None,
                            category=None, created=False)
    await state.set_state(Stock.name)
    data = await state.get_data()
    if kind == ItemKind.hardware:
        text = ("✅Начинаем добавление!\n\n✍Введите название фурнитуры.\n\n"
                "💡Например: Петля Blum с доводчиком")
    else:
        text = ("✅Начинаем добавление!\n\n✍Введите название материала.\n\n"
                "💡Рекомендуем к названию материала в конце дописывать цвет.")
    await message.answer(text, reply_markup=rk([_exit_btn(data)]))


@router.message(Stock.name, F.text)
async def new_name(message: Message, state: FSMContext):
    name = message.text.strip()[:200]
    if len(name) < 2:
        await message.answer("Название слишком короткое, напишите подробнее 🙂")
        return
    await state.update_data(new_name=name)
    data = await state.get_data()
    if data.get("kind") == ItemKind.hardware.value:
        await _set_type(message, state, T_HW)
        return
    await ask_type(message, state)


async def ask_type(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(Stock.mtype)
    await message.answer("✅Отлично\n\n✍Укажите тип материала:",
                         reply_markup=rk([T_BODY, T_HW], [T_FABRIC, T_PAINT], [BACK, _exit_btn(data)]))


@router.message(Stock.mtype, F.text == BACK)
async def type_back(message: Message, state: FSMContext):
    data = await state.get_data()
    await start_new(message, state, ItemKind(data["kind"]) if data.get("kind") else None, data.get("origin", "catalog"))


@router.message(Stock.mtype, F.text)
async def type_pick(message: Message, state: FSMContext):
    if message.text not in TYPES:
        await message.answer("Выберите тип кнопкой ниже 👇")
        return
    await _set_type(message, state, message.text)


async def _set_type(message: Message, state: FSMContext, t: str):
    kind, category, unit = TYPES[t]
    await state.update_data(kind=kind.value, category=category, unit=unit, mtype=t)
    data = await state.get_data()
    if category == "Корпусное покрытие":
        sizes = await call(message, lambda ctx: materials.recent_sheet_sizes(ctx)) or []
        for s in STD_SHEETS:
            if s not in sizes:
                sizes.append(s)
        await state.set_state(Stock.size)
        await message.answer("✅ Отлично\n\n✍Теперь укажите какая ширина и высота у одного листа этого материала\n\n"
                             "Например: 1220 × 2440 мм",
                             reply_markup=rk(*grid([f"{w} × {h}" for w, h in sizes[:6]], 2), [BACK, _exit_btn(data)]))
    elif unit is None:
        await state.set_state(Stock.unit)
        await message.answer("✅ Отлично\n\n✍В чём считать эту позицию?",
                             reply_markup=rk(UNITS_FOR[category], [BACK, _exit_btn(data)]))
    else:
        await ask_price(message, state)


@router.message(Stock.size, F.text == BACK)
@router.message(Stock.unit, F.text == BACK)
async def size_back(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("kind") == ItemKind.hardware.value and data.get("origin") == "order":
        await start_new(message, state, ItemKind.hardware, "order")
    else:
        await ask_type(message, state)


@router.message(Stock.size, F.text)
async def size_entered(message: Message, state: FSMContext):
    nums = [int(x) for x in re.findall(r"\d{2,5}", message.text)]
    if len(nums) != 2 or not all(100 <= n <= 10000 for n in nums):
        await message.answer("Укажите два размера в миллиметрах, например: 1220 × 2440")
        return
    await state.update_data(sheet=nums)
    await ask_price(message, state)


@router.message(Stock.unit, F.text)
async def unit_entered(message: Message, state: FSMContext):
    unit = message.text.strip()[:16]
    if not unit:
        return
    await state.update_data(unit=unit)
    await ask_price(message, state)


async def ask_price(message: Message, state: FSMContext):
    data = await state.get_data()
    prices = await call(message, lambda ctx: materials.recent_prices(ctx, category=data.get("category"))) or []
    await state.set_state(Stock.price)
    unit = data.get("unit") or "шт"
    if unit == "лист":
        text = "✅ Отлично\n\nУкажите пожалуйста стоимость одного листа из этого материала."
    else:
        text = f"✅ Отлично\n\nУкажите пожалуйста стоимость за 1 {UNIT_FORMS.get(unit, (unit,))[0]}."
    await message.answer(text, reply_markup=rk(*grid(price_buttons(prices), 3), [MANUAL], [BACK, _exit_btn(data)]))


@router.message(Stock.price, F.text == MANUAL)
async def price_manual(message: Message):
    await message.answer("✍ Напишите стоимость цифрами, например 440.000")


@router.message(Stock.price, F.text == BACK)
async def price_back(message: Message, state: FSMContext):
    data = await state.get_data()
    t = data.get("mtype")
    if t in (T_BODY, T_HW, T_PAINT):
        await _set_type(message, state, t)
    else:
        await ask_type(message, state)


@router.message(Stock.price, F.text)
async def price_entered(message: Message, state: FSMContext):
    price = read_money(message.text)
    if price is None:
        await message.answer("Напишите стоимость цифрами, например 660.000")
        return
    data = await state.get_data()
    attrs = {"sheet_w": data["sheet"][0], "sheet_h": data["sheet"][1]} if data.get("sheet") else None

    def work(ctx):
        m = materials.create(ctx, kind=ItemKind(data["kind"]), name=data["new_name"], unit=data["unit"] or "шт",
                             price=price, category=data.get("category") or "", attrs=attrs)
        return m.id, materials.count_kind(ctx, m.kind)
    res = await call(message, work, keyboard=rk([_exit_btn(data)]))
    if not res:
        return
    mid, number = res
    await state.update_data(material_id=mid, price=str(price), created=True, number=number)
    if _is_order(data):
        await ask_qty(message, state)
    else:
        await show_added(message, state)


# ================================================================ количество и итог

async def ask_qty(message: Message, state: FSMContext):
    data = await state.get_data()
    unit = data.get("unit") or "шт"
    product = data.get("product") or "изделие"
    if unit == "лист":
        q = f"📐 Сколько листов «{e(data['new_name'])}» понадобится для «{e(product)}»?"
    else:
        q = f"📐 Сколько ({UNIT_FORMS.get(unit, (unit,))[0]}) «{e(data['new_name'])}» нужно для «{e(product)}»?"
    await state.set_state(Stock.qty)
    await message.answer(("✅Отлично!\n\n" if data.get("created") else "") + q +
                         "\n\nМожно дробное число, например 1,5", reply_markup=numbers_kb([TO_ORDER]))


@router.message(Stock.qty, F.text == OTHER_NUMBER)
async def qty_other(message: Message):
    await message.answer("✍ Напишите количество цифрой, например 2,5")


@router.message(Stock.qty, F.text)
async def qty_entered(message: Message, state: FSMContext):
    q = read_qty(message.text)
    if q is None or q > 100000:
        await message.answer("Напишите количество цифрой, например 3")
        return
    data = await state.get_data()
    sheet = data.get("sheet") or [None, None]

    def work(ctx):
        item = orders.add_item(ctx, data["order_id"], kind=ItemKind(data["kind"]), material_id=data["material_id"],
                               quantity=q, height_mm=sheet[0], width_mm=sheet[1],
                               section=data.get("product") or "Изделие")
        return str(item.cost)
    cost = await call(message, work, keyboard=rk([TO_ORDER]))
    if cost is not None:
        await state.update_data(qty=str(q), cost=cost)
        await show_added(message, state)


async def show_added(message: Message, state: FSMContext):
    data = await state.get_data()
    unit = data.get("unit") or "шт"
    in_order = _is_order(data)
    lines = []
    if data.get("created"):
        hw = data.get("kind") == ItemKind.hardware.value
        lines += [f"{'🔩ФУРНИТУРА' if hw else '📦МАТЕРИАЛ'} №{data.get('number', 1)}", "",
                  f"Название: {e(data['new_name'])}"]
        if data.get("sheet"):
            lines.append(f"Размер: {data['sheet'][0]} × {data['sheet'][1]} мм")
        per = "" if unit == "лист" else f" за {UNIT_FORMS.get(unit, (unit,))[0]}"
        lines += [f"Стоимость: {money(data['price'])}{per}", "", "✅Успешно сохранился"]
    else:
        lines.append(f"✅ Добавлено: {e(data['new_name'])}")
    if in_order:
        lines += ["", f"🛋 В заказ «{e(data.get('product') or 'изделие')}»: {amount_text(data['qty'], unit)} — "
                      f"{money(data['cost'])}", "", "Вернётесь к заказу или добавить в заказ ещё материалы?"]
        kb = rk([ADD_MORE], [TO_ORDER_LONG])
    else:
        lines += ["", "Добавить ещё позицию в базу?"]
        kb = rk([ADD_MORE], [TO_STOCK, HOME])
    await state.set_state(Stock.added)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Stock.added, F.text == ADD_MORE)
async def add_more(message: Message, state: FSMContext):
    data = await state.get_data()
    if _is_order(data):
        await start_pick(message, state, ItemKind(data["kind"]))
    else:
        await start_new(message, state, None, "catalog")


# ================================================================ услуга настройки базы

def _admin_chat_ids(db) -> set[int]:
    ids = set(get_settings().superadmin_ids)
    ids |= {u for u in db.scalars(select(User.telegram_id).where(User.is_superadmin.is_(True))) if u}
    return ids


async def start_setup(message: Message, state: FSMContext):
    data = await state.get_data()

    def work(ctx):
        us = tariffs.usage(ctx.db, ctx.eid)
        return (ctx.enterprise.name, us.get("plan").title if us.get("plan") else None, us["materials"],
                us.get("materials_limit"))
    res = await call(message, work)
    if not res:
        return
    ent, plan, used, limit = res
    if limit:
        lim = (f"📦 Ваш тариф {plan}: до {limit} позиций в базе, сейчас занято {used} — "
               f"можно добавить ещё {max(0, limit - used)}. Позиции сверх лимита добавлены не будут.")
    else:
        lim = f"🔒 Подписка не активна — оформите её в «{BTN_SUB}», иначе позиции не получится добавить в базу."
    await state.set_state(Stock.setup)
    await state.update_data(setup_count=0, setup_header=False)
    await message.answer(
        f"✅ Отлично! Услуга настройки базы — {money(get_settings().setup_service_price)}.\n\n"
        "📎 Отправьте сюда весь каталог материалов и фурнитуры с ценами: файлом (Excel, PDF), фотографиями "
        "или списком текстом — несколькими сообщениями, если нужно.\n\n"
        f"{lim}\n\nКогда отправите всё — нажмите «{SETUP_DONE}».",
        reply_markup=rk([SETUP_DONE], [_exit_btn(data)]))


@router.message(Stock.setup, F.text == SETUP_DONE)
async def setup_done(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("setup_count"):
        await message.answer("Пока ничего не получено — отправьте каталог файлом, фото или текстом 🙂")
        return

    def work(ctx):
        log(ctx.db, "stock.setup_requested", actor_id=ctx.user.id, enterprise_id=ctx.eid,
            messages=data["setup_count"])
        return True
    await call(message, work)
    text = ("✅ Заявка на настройку отправлена!\n\nСпециалист SmetaPro свяжется с вами, чтобы согласовать оплату "
            f"({money(get_settings().setup_service_price)}), и заполнит вашу базу.")
    if _is_order(data):
        from app.bot.handlers.calc import show_card
        await show_card(message, state, text)
    else:
        await materials_menu(message, state, text)


@router.message(Stock.setup)
async def setup_material(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if message.text and message.text.startswith("/"):
        return

    def work(ctx):
        us = tariffs.usage(ctx.db, ctx.eid)
        return (_admin_chat_ids(ctx.db), ctx.enterprise.name, ctx.user.telegram_username,
                us.get("plan").title if us.get("plan") else "нет подписки", us["materials"], us.get("materials_limit"))
    res = await call(message, work)
    if not res:
        return
    admins, ent, username, plan, used, limit = res
    for chat_id in admins:
        try:
            if not data.get("setup_header"):
                await bot.send_message(chat_id, f"🛠 <b>Заявка на настройку базы</b>\nПредприятие: {e(ent)}\n"
                                                f"Пользователь: {'@' + e(username) if username else ''} "
                                                f"(ID {message.from_user.id})\nТариф: {plan}, база {used}"
                                                f"{' из ' + str(limit) if limit else ''}\nКаталог — ниже:")
            await bot.copy_message(chat_id, message.chat.id, message.message_id)
        except Exception:
            pass
    await state.update_data(setup_count=data.get("setup_count", 0) + 1, setup_header=True)
    await message.answer(f"📥 Получено. Можно отправить ещё или нажать «{SETUP_DONE}».")


# ================================================================ раздел «📦 Материалы»

async def materials_menu(message: Message, state: FSMContext, prefix: str = ""):
    await state.clear()

    def work(ctx: Ctx):
        ctx.require("materials.view")
        us = tariffs.usage(ctx.db, ctx.eid)
        return (len(materials.list_materials(ctx, kind=ItemKind.material)),
                len(materials.list_materials(ctx, kind=ItemKind.hardware)), us.get("materials_limit"),
                ctx.can("materials.edit"))
    res = await call(message, work, keyboard=main_menu())
    if not res:
        return
    mats, hw, limit, can_edit = res
    lim = f" из {limit}" if limit else ""
    rows = [[M_MATS, M_HW]]
    if can_edit:
        rows += [[M_ADD], [M_BUY]]
    rows.append([HOME])
    await state.set_state(Stock.menu)
    await message.answer((prefix + "\n\n" if prefix else "") +
                         f"📦 <b>База материалов</b>\n\nМатериалов: {mats}\nФурнитуры: {hw}\n"
                         f"Всего позиций: {mats + hw}{lim}", reply_markup=rk(*rows))


@router.message(F.text == BTN_MATERIALS)
async def materials_entry(message: Message, state: FSMContext):
    await materials_menu(message, state)


@router.message(F.text == TO_STOCK)
async def to_stock(message: Message, state: FSMContext):
    await materials_menu(message, state)


@router.message(Stock.menu, F.text == M_ADD)
async def menu_add(message: Message, state: FSMContext):
    await start_new(message, state, None, "catalog")


@router.message(Stock.menu, F.text == M_BUY)
async def menu_buy(message: Message, state: FSMContext):
    await state.update_data(origin="catalog")
    await start_setup(message, state)


@router.message(Stock.menu, F.text.in_({M_MATS, M_HW}))
async def menu_list(message: Message, state: FSMContext):
    await show_list(message, state, ItemKind.material if message.text == M_MATS else ItemKind.hardware)


async def show_list(message: Message, state: FSMContext, kind: ItemKind, needle: str | None = None):
    def work(ctx):
        return [(m.id, _label(m)) for m in materials.list_materials(ctx, kind=kind)
                if not needle or needle in m.name.lower()]
    items = await call(message, work)
    if items is None:
        return
    await state.set_state(Stock.list)
    await state.update_data(list_kind=kind.value, list_map={t: mid for mid, t in items[:40]})
    if not items:
        await message.answer("Ничего не найдено." if needle else "Здесь пока пусто.",
                             reply_markup=rk([TO_STOCK, HOME]))
        return
    title = "📦 Материалы" if kind == ItemKind.material else "🔩 Фурнитура"
    hint = "\n🔎 Можно написать часть названия для поиска." if len(items) > 10 else ""
    await message.answer(f"{title} ({len(items)}). Выберите позицию:{hint}",
                         reply_markup=rk(*[[t] for _, t in items[:40]], [TO_STOCK, HOME]))


@router.message(Stock.list, F.text)
async def list_pick(message: Message, state: FSMContext):
    data = await state.get_data()
    mid = data.get("list_map", {}).get(message.text)
    if not mid:
        await show_list(message, state, ItemKind(data["list_kind"]), message.text.strip().lower())
        return
    await state.update_data(item_id=mid)
    await show_item(message, state)


async def show_item(message: Message, state: FSMContext, prefix: str = ""):
    mid = (await state.get_data())["item_id"]

    def work(ctx):
        m = materials.get(ctx, mid)
        size = f"\nРазмер: {m.attrs['sheet_w']} × {m.attrs['sheet_h']} мм" if (m.attrs or {}).get("sheet_w") else ""
        unit = UNIT_FORMS.get(m.unit, (m.unit,))[0]
        return (f"<b>{e(m.name)}</b>\nТип: {e(m.category) or '—'}{size}\n"
                f"Стоимость: {money(m.price)} за {unit}"), ctx.can("materials.edit"), m.category
    res = await call(message, work)
    if not res:
        return
    text, can_edit, category = res
    await state.set_state(Stock.item)
    await state.update_data(category=category)
    rows = [[I_PRICE, I_DELETE]] if can_edit else []
    await message.answer((prefix + "\n\n" if prefix else "") + text, reply_markup=rk(*rows, [I_BACK, HOME]))


@router.message(Stock.item, F.text == I_BACK)
async def item_back(message: Message, state: FSMContext):
    await show_list(message, state, ItemKind((await state.get_data())["list_kind"]))


@router.message(Stock.item, F.text == I_PRICE)
async def item_price(message: Message, state: FSMContext):
    data = await state.get_data()
    prices = await call(message, lambda ctx: materials.recent_prices(ctx, category=data.get("category"))) or []
    await state.set_state(Stock.item_price)
    await message.answer("💵 Новая стоимость за единицу. Выберите или напишите:",
                         reply_markup=rk(*grid(price_buttons(prices), 3), [I_BACK]))


@router.message(Stock.item_price, F.text)
async def item_price_set(message: Message, state: FSMContext):
    if message.text == I_BACK:
        await show_item(message, state)
        return
    price = read_money(message.text)
    if price is None:
        await message.answer("Напишите стоимость цифрами, например 660.000")
        return
    mid = (await state.get_data())["item_id"]
    if await call(message, lambda ctx: materials.update(ctx, mid, price=price).id):
        await show_item(message, state, "✅ Цена обновлена. Новые расчёты будут по новой цене.")


@router.message(Stock.item, F.text == I_DELETE)
async def item_delete(message: Message, state: FSMContext):
    await state.set_state(Stock.item_delete)
    await message.answer("🗑 Удалить эту позицию из базы?", reply_markup=rk([YES, NO]))


@router.message(Stock.item_delete, F.text)
async def item_delete_confirm(message: Message, state: FSMContext):
    if message.text != YES:
        await show_item(message, state)
        return
    data = await state.get_data()
    res = await call(message, lambda ctx: materials.delete(ctx, data["item_id"]))
    if res:
        await show_list(message, state, ItemKind(data["list_kind"]))
        if res == "archived":
            await message.answer("📦 Позиция используется в заказах, поэтому перенесена в архив: старые расчёты "
                                 "сохранены, в новых она не появится.")


_ = plural
