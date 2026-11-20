"""Перевод бота на лету.

Исходящие: middleware сессии Telegram переводит текст, подпись и кнопки каждого запроса
на язык получателя (кэш язык ← Telegram ID). Входящие: нажатая кнопка на узбекском/казахском
заменяется исходной русской, поэтому обработчики работают с одними константами.
"""
from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app import i18n
from app.db import session_scope
from app.models import User


def _translate_keyboard(markup: ReplyKeyboardMarkup, lang: str) -> ReplyKeyboardMarkup:
    rows = []
    for row in markup.keyboard:
        new_row = []
        for b in row:
            t = i18n.tr(b.text, lang)
            i18n.remember_button(b.text, t)
            new_row.append(KeyboardButton(text=t))
        rows.append(new_row)
    return markup.model_copy(update={
        "keyboard": rows,
        "input_field_placeholder": i18n.tr(markup.input_field_placeholder, lang),
    })


class TranslateOutgoing(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        lang = i18n.user_lang(chat_id) if isinstance(chat_id, int) else None
        if lang and lang != i18n.DEFAULT:
            update = {}
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if isinstance(value, str):
                    update[field] = i18n.tr(value, lang)
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, ReplyKeyboardMarkup):
                update["reply_markup"] = _translate_keyboard(markup, lang)
            if update:
                method = method.model_copy(update=update)
        return await make_request(bot, method)


def _load_lang(tg_id: int) -> str:
    with session_scope() as db:
        lang = db.query(User.language).filter(User.telegram_id == tg_id).scalar()
    return i18n.normalize(lang)


class LanguageIncoming(BaseMiddleware):
    """Язык пользователя в кэш; нажатая переведённая кнопка → исходная русская."""

    async def __call__(self, handler, event: Message, data):
        user = event.from_user
        if user and i18n.user_lang(user.id) is None:
            i18n.set_user_lang(user.id, await asyncio.to_thread(_load_lang, user.id))
        if event.text:
            source = i18n.to_source(event.text)
            if source != event.text:
                event = event.model_copy(update={"text": source})
        return await handler(event, data)
