"""/start, регистрация предприятия, deep-link'и (ref_, inv_, login_), веб-кабинет."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.core import BTN_WEB, answer, call, e, kb, main_menu
from app.config import get_settings
from app.core.errors import DomainError
from app.core.permissions import ROLE_TITLES
from app.db import session_scope
from app.services import accounts, auth_web, staff, tariffs

router = Router(name="start")


class Reg(StatesGroup):
    name = State()
    phone = State()
    address = State()


def _status_text(ctx) -> str:
    if not ctx.enterprise:
        return ""
    us = tariffs.usage(ctx.db, ctx.eid)
    sub = us.get("subscription")
    role = ROLE_TITLES[ctx.role]
    if sub:
        return (f"🏭 <b>{e(ctx.enterprise.name)}</b> · {role}\n"
                f"Тариф <b>{us['plan'].title}</b> до {sub.ends_at:%d.%m.%Y} · смет {us['estimates_used']}/{us['estimates_limit']}")
    return f"🏭 <b>{e(ctx.enterprise.name)}</b> · {role}\n🔒 Нет активной подписки — откройте «💳 Подписка»."


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = (command.args or "").strip()
    tg = message.from_user

    if arg.startswith("login_"):
        await message.answer("🔐 Вход на сайт Furniture Estimate.\nЕсли это вы открыли страницу входа — подтвердите.",
                             reply_markup=kb([("✅ Подтвердить вход", f"login:{arg[6:]}")], [("Отмена", "noop")]))
        return

    ref = arg[4:] if arg.startswith("ref_") else None

    def work(ctx):
        # Привязка к рефереру — только для новых пользователей без предприятия
        if ref and not ctx.user.referred_by_id and not ctx.enterprise:
            accounts.attach_referral(ctx.db, ctx.user, ref)
        joined = None
        if arg.startswith("inv_"):
            m = staff.accept(ctx.db, ctx.user, arg[4:])
            joined = (m.enterprise.name, ROLE_TITLES[m.role])
            ctx = accounts.load_ctx(ctx.db, ctx.user)
        return bool(ctx.enterprise), _status_text(ctx), joined

    res = await call(message, work, need_enterprise=False)
    if res is None:
        return
    has_ent, status, joined = res
    if joined:
        await message.answer(f"👥 Вы присоединились к «{e(joined[0])}» как {joined[1].lower()}.")
    if has_ent:
        await message.answer(f"Здравствуйте, {e(tg.first_name)}!\n\n{status}", reply_markup=main_menu())
        return
    await message.answer(
        "👋 <b>Furniture Estimate</b> — расчёт стоимости мебели, PDF-сметы, затраты и аналитика для вашего цеха.\n\n"
        "Давайте зарегистрируем предприятие. Как оно называется?")
    await state.set_state(Reg.name)


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 200:
        await message.answer("Название — от 2 до 200 символов. Попробуйте ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    await message.answer("Телефон предприятия (для сметы). Или «-», чтобы пропустить.")


@router.message(Reg.phone, F.text)
async def reg_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=None if phone == "-" else phone[:32])
    await state.set_state(Reg.address)
    await message.answer("Адрес предприятия. Или «-», чтобы пропустить.")


@router.message(Reg.address, F.text)
async def reg_address(message: Message, state: FSMContext):
    data = await state.get_data()
    address = None if message.text.strip() == "-" else message.text.strip()

    def work(ctx):
        accounts.register_enterprise(ctx.db, ctx.user, name=data["name"], phone=data.get("phone"), address=address)
        return True

    if await call(message, work, need_enterprise=False):
        await state.clear()
        await message.answer(
            f"✅ Предприятие «{e(data['name'])}» зарегистрировано!\n\n"
            "Дальше:\n1. «💳 Подписка» — выберите тариф и оплатите.\n"
            "2. «⚙️ Настройки» — задайте наценки.\n3. «📦 Материалы» — заполните базу.\n"
            "4. «🧮 Новый расчёт» — первая смета.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("login:"))
async def login_confirm(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]

    def work(ctx):
        auth_web.confirm_login(ctx.db, ctx.user, token)
        return True

    if await call(cq, work, need_enterprise=False):
        await answer(cq, "✅ Вход подтверждён. Вернитесь в браузер — страница откроется автоматически.", edit=True)


@router.callback_query(F.data == "noop")
async def noop(cq: CallbackQuery):
    await cq.answer()
    try:
        await cq.message.delete()
    except Exception:
        pass


@router.message(F.text == BTN_WEB)
@router.message(Command("web"))
async def web(message: Message):
    s = get_settings()
    await message.answer("🌐 Веб-кабинет: подписка, платежи, сотрудники, аналитика и сметы.\n"
                         "На сайте нажмите «Войти через Telegram» и подтвердите вход здесь.",
                         reply_markup=kb([("Открыть кабинет", f"{s.base_url.rstrip('/')}/login")]))


@router.message(Command("menu"))
async def menu(message: Message, state: FSMContext):
    await state.clear()
    status = await call(message, _status_text)
    if status is not None:
        await message.answer(status, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(Command("help"))
async def help_(message: Message):
    await message.answer(
        "<b>Команды</b>\n/menu — главное меню\n/cancel — отменить текущее действие\n/web — веб-кабинет\n"
        "/admin — панель администратора платформы\n\nПодробнее: " + get_settings().base_url.rstrip('/') + "/faq")


_ = (DomainError, session_scope)
