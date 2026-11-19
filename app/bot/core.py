"""Общие помощники бота. Бизнес-логики здесь нет — только вызов сервисного слоя (app.services),
который используют и сайт, и бот."""
from __future__ import annotations

import asyncio
import html
from typing import Any, Callable

from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, User as TgUser)

from app.core.errors import DomainError, FeatureUnavailable, LimitReached, SubscriptionRequired
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
MENU_BUTTONS = {BTN_NEW, BTN_ORDERS, BTN_MATERIALS, BTN_EXPENSES, BTN_ANALYTICS, BTN_STAFF, BTN_SUB, BTN_REF,
                BTN_SETTINGS, BTN_WEB}


def main_menu() -> ReplyKeyboardMarkup:
    rows = [[BTN_NEW, BTN_ORDERS], [BTN_MATERIALS, BTN_EXPENSES], [BTN_ANALYTICS, BTN_STAFF],
            [BTN_SUB, BTN_REF], [BTN_SETTINGS, BTN_WEB]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True,
                               is_persistent=True)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """kb([("Текст", "callback"), ...], [...]). Если callback начинается с http — это ссылка."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=d) if d.startswith("http") else InlineKeyboardButton(text=t, callback_data=d)
         for t, d in row] for row in rows if row])


def e(s: Any) -> str:
    return html.escape(str(s or ""))


# ---------------------------------------------------------------- вызов сервисов

def _identity(tg: TgUser) -> dict:
    return dict(username=tg.username, full_name=tg.full_name or "")


def _work(tg: TgUser, fn: Callable[[Ctx], Any], need_enterprise: bool):
    with session_scope() as db:
        user, _ = accounts.get_or_create_telegram_user(db, tg.id, **_identity(tg))
        ctx = accounts.load_ctx(db, user)
        if need_enterprise and not ctx.enterprise:
            raise NoEnterprise()
        return fn(ctx)


class NoEnterprise(Exception):
    pass


async def answer(event: Message | CallbackQuery, text: str, markup=None, edit: bool = False):
    msg = event.message if isinstance(event, CallbackQuery) else event
    if isinstance(event, CallbackQuery):
        await event.answer()
        if edit:
            try:
                return await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            except Exception:
                pass
    return await msg.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def call(event: Message | CallbackQuery, fn: Callable[[Ctx], Any], need_enterprise: bool = True):
    """Выполнить fn(ctx) в отдельном потоке в транзакции. Доменные ошибки показываются пользователю,
    транзакция при этом откатывается. Возвращает None при ошибке."""
    try:
        return await asyncio.to_thread(_work, event.from_user, fn, need_enterprise)
    except NoEnterprise:
        await answer(event, "Сначала зарегистрируйте предприятие — отправьте /start.")
    except (FeatureUnavailable, LimitReached, SubscriptionRequired) as err:
        await answer(event, f"⚠️ {e(err.message)}", kb([("💳 Тарифы и оплата", "sub:menu")]))
    except DomainError as err:
        await answer(event, f"⚠️ {e(err.message)}")
    return None


async def call_db(fn: Callable, *args):
    """Выполнить fn(db, *args) без пользовательского контекста (админ-операции, системные)."""
    def work():
        with session_scope() as db:
            return fn(db, *args)
    return await asyncio.to_thread(work)


def parse_num(text: str):
    from app.core.fmt import D
    try:
        return D(text.strip().replace(" ", "").replace(" ", ""))
    except Exception:
        raise DomainError("Введите число, например 125000 или 2,5.")
