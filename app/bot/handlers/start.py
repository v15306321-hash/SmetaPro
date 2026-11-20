"""/start, регистрация предприятия, deep-link'и (ref_, inv_, login_), главное меню, веб-кабинет."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from app import i18n
from app.bot.core import BTN_LANG, BTN_SUB, BTN_WEB, HOME, SKIP, call, e, main_menu, rk
from app.config import get_settings
from app.core.permissions import ROLE_TITLES
from app.services import accounts, auth_web, promo, staff, tariffs

router = Router(name="start")

CONFIRM_LOGIN = "✅ Подтвердить вход"
CANCEL_LOGIN = "❌ Это не я"


class Reg(StatesGroup):
    name = State()
    phone = State()
    address = State()


class Login(StatesGroup):
    confirm = State()


class Lang(StatesGroup):
    pick = State()


def status_text(ctx) -> str:
    if not ctx.enterprise:
        return ""
    us = tariffs.usage(ctx.db, ctx.eid)
    sub = us.get("subscription")
    role = ROLE_TITLES[ctx.role]
    if sub:
        return (f"🏭 <b>{e(ctx.enterprise.name)}</b> · {role}\n"
                f"Тариф <b>{us['plan'].title}</b> до {sub.ends_at:%d.%m.%Y} · "
                f"сметы {us['estimates_used']} из {us['estimates_limit']}")
    if not ctx.is_owner:
        return f"🏭 <b>{e(ctx.enterprise.name)}</b> · {role}\n🔒 Подписка предприятия не активна — сообщите владельцу."
    return (f"🏭 <b>{e(ctx.enterprise.name)}</b> · {role}\n"
            f"🔒 Подписка не активна — откройте «{BTN_SUB}».")


def _role(ctx) -> str | None:
    return ctx.role.value if ctx.role else None


async def show_home(message: Message, state: FSMContext, greeting: str | None = None):
    await state.clear()
    res = await call(message, lambda ctx: (status_text(ctx), _role(ctx)))
    if res is not None:
        status, role = res
        await message.answer((greeting + "\n\n" if greeting else "🏛️ Главное меню\n\n") + status,
                             reply_markup=main_menu(role))


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    arg = (command.args or "").strip()

    if arg.startswith("login_"):
        await state.set_state(Login.confirm)
        await state.update_data(token=arg[6:])
        await message.answer("🔐 Вход на сайт SmetaPro.\n\nЕсли это вы сейчас открыли страницу входа — подтвердите.",
                             reply_markup=rk([CONFIRM_LOGIN], [CANCEL_LOGIN]))
        return

    ref = arg[4:] if arg.startswith("ref_") else None

    def work(ctx):
        if ref and not ctx.user.referred_by_id and not ctx.enterprise:
            accounts.attach_referral(ctx.db, ctx.user, ref)
        joined = None
        if arg.startswith("inv_"):
            m = staff.accept(ctx.db, ctx.user, arg[4:])
            joined = (m.enterprise.name, ROLE_TITLES[m.role])
            ctx = accounts.load_ctx(ctx.db, ctx.user)
        return bool(ctx.enterprise), status_text(ctx), joined, _role(ctx)

    res = await call(message, work, need_enterprise=False)
    if res is None:
        return
    has_ent, status, joined, role = res
    if joined:
        await message.answer(f"👥 Вы присоединились к «{e(joined[0])}» как {joined[1].lower()}.")
    deal = promo.banner()
    deal_text = f"\n\n{deal}" if deal else ""
    if has_ent:
        await message.answer(f"Здравствуйте, {e(message.from_user.first_name)}!\n\n{status}{deal_text}",
                             reply_markup=main_menu(role))
        return
    # Новый пользователь: сначала язык, затем регистрация предприятия
    await ask_language(message, state, after="register")


async def send_welcome(message: Message, state: FSMContext):
    deal = promo.banner()
    deal_text = f"\n\n{deal}" if deal else ""
    await message.answer(
        "👋 Добро пожаловать в <b>SmetaPro</b>!\n\n"
        "Здесь вы считаете стоимость мебели, отправляете клиентам PDF-сметы и видите, сколько зарабатывает цех."
        f"{deal_text}\n\n"
        "Давайте зарегистрируем ваше предприятие.\n\n🏭 Как оно называется?",
        reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.name)


# ---------------------------------------------------------------- язык

LANG_PROMPT = "🌐 Выберите язык · Tilni tanlang · Тілді таңдаңыз"
LANG_BY_BUTTON = {title: code for code, title in i18n.LANGS.items()}


async def ask_language(message: Message, state: FSMContext, after: str = "home"):
    await state.set_state(Lang.pick)
    await state.update_data(lang_after=after)
    await message.answer(LANG_PROMPT, reply_markup=rk(*[[t] for t in i18n.LANGS.values()]))


@router.message(F.text == BTN_LANG)
@router.message(Command("lang"))
async def lang_menu(message: Message, state: FSMContext):
    await ask_language(message, state)


@router.message(F.text.in_(set(LANG_BY_BUTTON)))
async def lang_chosen(message: Message, state: FSMContext):
    code = LANG_BY_BUTTON[message.text]
    after = (await state.get_data()).get("lang_after", "home")

    def work(ctx):
        ctx.user.language = code
        return True
    if await call(message, work, need_enterprise=False):
        i18n.set_user_lang(message.from_user.id, code)
        if after == "register":
            await send_welcome(message, state)
        else:
            await show_home(message, state, "✅ Язык изменён")


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 2 <= len(name) <= 200:
        await message.answer("Название должно быть от 2 до 200 символов. Попробуйте ещё раз 🙂")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.phone)
    await message.answer("✅Отлично!\n\n📞 Укажите телефон предприятия — он будет в сметах. (Или пропустить)",
                         reply_markup=rk([SKIP]))


@router.message(Reg.phone, F.text)
async def reg_phone(message: Message, state: FSMContext):
    phone = None if message.text == SKIP else message.text.strip()[:32]
    await state.update_data(phone=phone)
    await state.set_state(Reg.address)
    await message.answer("✅Отлично!\n\n📍 Укажите адрес предприятия. (Или пропустить)", reply_markup=rk([SKIP]))


@router.message(Reg.address, F.text)
async def reg_address(message: Message, state: FSMContext):
    data = await state.get_data()
    address = None if message.text == SKIP else message.text.strip()

    def work(ctx):
        accounts.register_enterprise(ctx.db, ctx.user, name=data["name"], phone=data.get("phone"), address=address)
        return True

    if await call(message, work, need_enterprise=False):
        await state.clear()
        await message.answer(
            f"✅ Предприятие «{e(data['name'])}» зарегистрировано!\n\n"
            "Что дальше:\n"
            f"1️⃣ «{BTN_SUB}» — выберите тариф и оплатите.\n"
            "2️⃣ «⚙️ Настройки» — задайте наценки.\n"
            "3️⃣ «🧮 Новый расчёт» — первая смета.", reply_markup=main_menu())


# ---------------------------------------------------------------- вход на сайт

@router.message(Login.confirm, F.text == CONFIRM_LOGIN)
async def login_confirm(message: Message, state: FSMContext):
    token = (await state.get_data()).get("token", "")

    def work(ctx):
        auth_web.confirm_login(ctx.db, ctx.user, token)
        return True

    if await call(message, work, need_enterprise=False):
        await state.clear()
        await message.answer("✅ Вход подтверждён. Вернитесь в браузер — кабинет откроется автоматически.",
                             reply_markup=main_menu())


@router.message(Login.confirm, F.text == CANCEL_LOGIN)
async def login_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Хорошо, вход не подтверждён. Если это были не вы — просто ничего не делайте.",
                         reply_markup=main_menu())


# ---------------------------------------------------------------- навигация

@router.message(F.text == HOME)
@router.message(Command("menu"))
async def home(message: Message, state: FSMContext):
    await show_home(message, state)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await show_home(message, state, "Действие отменено.")


@router.message(F.text == BTN_WEB)
@router.message(Command("web"))
async def web(message: Message, state: FSMContext):
    await state.clear()
    s = get_settings()
    await message.answer(
        "🌐 <b>Веб-кабинет SmetaPro</b>\n\nПодписка, платежи, сотрудники, аналитика с графиками и все сметы.\n\n"
        f"Откройте {s.base_url.rstrip('/')}/login и нажмите «Войти через Telegram» — "
        "подтверждение придёт сюда.", reply_markup=main_menu(), disable_web_page_preview=True)


@router.message(Command("help"))
async def help_(message: Message):
    await message.answer(
        "<b>Команды</b>\n/menu — главное меню\n/cancel — отменить текущее действие\n/web — веб-кабинет\n"
        "/admin — панель администратора платформы\n\n"
        f"Помощь: {get_settings().base_url.rstrip('/')}/faq", disable_web_page_preview=True)
