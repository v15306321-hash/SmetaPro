"""Общие помощники бота SmetaPro. Бизнес-логики здесь нет — только вызов сервисного слоя (app.services),
который используют и сайт, и бот.

Интерфейс бота — только кнопки мини-меню (клавиатура под полем ввода), без кнопок под сообщениями.
Каждый шаг задаёт один вопрос; кнопки подсказывают ответ, но всегда можно написать своё.
"""
from __future__ import annotations

import asyncio
import html
from decimal import Decimal
from typing import Any, Callable

from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, User as TgUser

from app.core.errors import DomainError, FeatureUnavailable, LimitReached, SubscriptionRequired
from app.core.fmt import money, parse_money, parse_qty, qty
from app.db import session_scope
from app.services import accounts
from app.services.accounts import Ctx

# ---------------------------------------------------------------- главное меню
BTN_NEW = "🧮 Новый расчёт"
BTN_ORDERS = "📋 Заказы"
BTN_MATERIALS = "📦 Материалы"
BTN_EXPENSES = "💸 Затраты"
BTN_ANALYTICS = "📊 Аналитика"
BTN_STAFF = "👥 Сотрудники"
BTN_SUB = "💳 Подписка"
BTN_REF = "🎁 Пригласить друга"
BTN_SETTINGS = "⚙️ Настройки"
BTN_WEB = "🌐 Веб-кабинет"
HOME = "🏛️Главная"
BTN_LEAVE = "🚪 Уйти из цеха"
BTN_SUPPORT = "👨‍💻 Тех. поддержка"
BTN_LANG = "🌐 Язык"

# Кнопки, которые прерывают любой незавершённый диалог
MENU_BUTTONS = {BTN_NEW, BTN_ORDERS, BTN_MATERIALS, BTN_EXPENSES, BTN_ANALYTICS, BTN_STAFF, BTN_SUB, BTN_REF,
                BTN_SETTINGS, BTN_WEB, HOME, BTN_LEAVE, BTN_SUPPORT, BTN_LANG}

# ---------------------------------------------------------------- общие кнопки шагов
SKIP = "⏩Пропустить"
BACK = "↩️Назад"
TO_ORDER = "🔙Заказ"
TO_ORDER_LONG = "🔙Вернуться к заказу"
OTHER_NUMBER = "Другое число📝"
MANUAL = "✍Ввести вручную"
YES = "✅ Да"
NO = "❌ Нет"


def rk(*rows: list[str], placeholder: str | None = None) -> ReplyKeyboardMarkup:
    """Клавиатура мини-меню. Пустые строки пропускаются."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows if row],
                               resize_keyboard=True, is_persistent=True, input_field_placeholder=placeholder)


def main_menu(role: str | None = None) -> ReplyKeyboardMarkup:
    """Главное меню. Сотруднику (role="employee") — только расчёты, без подписки и управления."""
    if role == "employee":
        return rk([BTN_NEW, BTN_ORDERS], [BTN_MATERIALS, BTN_WEB], [BTN_SUPPORT, BTN_LANG], [BTN_LEAVE],
                  placeholder="Выберите раздел")
    return rk([BTN_NEW, BTN_ORDERS], [BTN_MATERIALS, BTN_EXPENSES], [BTN_ANALYTICS, BTN_STAFF],
              [BTN_SUB, BTN_REF], [BTN_SETTINGS, BTN_WEB], [BTN_SUPPORT, BTN_LANG], placeholder="Выберите раздел")


def numbers_kb(*extra_rows: list[str], upto: int = 10) -> ReplyKeyboardMarkup:
    nums = [str(i) for i in range(1, upto + 1)]
    return rk(nums[:5], nums[5:10], [OTHER_NUMBER], *extra_rows)


def grid(items: list[str], cols: int = 2) -> list[list[str]]:
    return [items[i:i + cols] for i in range(0, len(items), cols)]


def price_buttons(prices: list) -> list[str]:
    return [money(p, "").strip() for p in prices]


def e(s: Any) -> str:
    return html.escape(str(s or ""))


def plural(n, forms: tuple[str, str, str]) -> str:
    try:
        v = Decimal(str(n))
    except Exception:
        return forms[2]
    if v != v.to_integral_value():
        return forms[1]
    k = int(v) % 100
    if 11 <= k <= 19:
        return forms[2]
    k %= 10
    return forms[0] if k == 1 else forms[1] if 2 <= k <= 4 else forms[2]


UNIT_FORMS = {
    "лист": ("лист", "листа", "листов"),
    "шт": ("шт", "шт", "шт"),
    "компл": ("компл.", "компл.", "компл."),
    "уп": ("уп.", "уп.", "уп."),
    "пара": ("пара", "пары", "пар"),
    "м.п.": ("м.п.", "м.п.", "м.п."),
    "м²": ("м²", "м²", "м²"),
    "л": ("л", "л", "л"),
    "кг": ("кг", "кг", "кг"),
    "услуга": ("услуга", "услуги", "услуг"),
    "чел.": ("чел.", "чел.", "чел."),
}


def amount_text(value, unit: str) -> str:
    """«3 листа», «1,5 листа», «10 шт»."""
    return f"{qty(value)} {plural(value, UNIT_FORMS.get(unit, (unit, unit, unit)))}"


def read_money(text: str) -> Decimal | None:
    try:
        v = parse_money(text)
        return v if v >= 0 else None
    except ValueError:
        return None


def read_qty(text: str) -> Decimal | None:
    try:
        v = parse_qty(text)
        return v if v > 0 else None
    except ValueError:
        return None


# ---------------------------------------------------------------- вызов сервисов

class NoEnterprise(Exception):
    pass


def _work(tg: TgUser, fn: Callable[[Ctx], Any], need_enterprise: bool):
    with session_scope() as db:
        user, _ = accounts.get_or_create_telegram_user(db, tg.id, username=tg.username,
                                                       full_name=tg.full_name or "")
        ctx = accounts.load_ctx(db, user)
        if need_enterprise and not ctx.enterprise:
            raise NoEnterprise()
        return fn(ctx)


async def call(message: Message, fn: Callable[[Ctx], Any], need_enterprise: bool = True, keyboard=None):
    """Выполнить fn(ctx) в отдельном потоке в одной транзакции. Понятная ошибка — пользователю
    (транзакция откатывается), клавиатура при этом не меняется. При ошибке возвращает None."""
    try:
        return await asyncio.to_thread(_work, message.from_user, fn, need_enterprise)
    except NoEnterprise:
        await message.answer("Сначала зарегистрируйте предприятие — отправьте /start.")
    except SubscriptionRequired as err:
        await message.answer(f"🔒 {e(err.message)}\n\nОформить подписку — раздел «{BTN_SUB}».",
                             reply_markup=keyboard)
    except (FeatureUnavailable, LimitReached) as err:
        await message.answer(f"⚠️ {e(err.message)}\n\nСменить тариф — раздел «{BTN_SUB}».", reply_markup=keyboard)
    except DomainError as err:
        await message.answer(f"⚠️ {e(err.message)}", reply_markup=keyboard)
    return None


async def call_db(fn: Callable, *args):
    """Выполнить fn(db, *args) без пользовательского контекста (админ-операции, системные)."""
    def work():
        with session_scope() as db:
            return fn(db, *args)
    return await asyncio.to_thread(work)
