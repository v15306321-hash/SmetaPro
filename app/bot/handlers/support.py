"""«👨‍💻 Тех. поддержка»: обращения пересылаются администраторам платформы,
ответ администратора (reply на пересланное сообщение) доставляется пользователю."""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from app.bot.core import BTN_SUPPORT, HOME, call, call_db, e, rk
from app.config import get_settings
from app.models import User

router = Router(name="support")

SUPPORT_DONE = "✅ Вопрос решён"
TAG = re.compile(r"#u(\d+)")


class Support(StatesGroup):
    chat = State()


def _admin_ids(db) -> set[int]:
    ids = set(get_settings().superadmin_ids)
    ids |= {u for u in db.scalars(select(User.telegram_id).where(User.is_superadmin.is_(True))) if u}
    return ids


# ---------------------------------------------------------------- ответ администратора (первым — до других хендлеров)

async def _is_admin_reply(message: Message) -> bool:
    r = message.reply_to_message
    if not r or not r.from_user or not r.from_user.is_bot:
        return False
    return bool(TAG.search((r.text or "") + (r.caption or "")))


@router.message(_is_admin_reply)
async def admin_answer(message: Message, bot: Bot):
    r = message.reply_to_message
    user_tg = int(TAG.search((r.text or "") + (r.caption or "")).group(1))
    admins = await call_db(_admin_ids)
    if message.from_user.id not in admins:
        return
    try:
        if message.text:
            await bot.send_message(user_tg, f"👨‍💻 <b>Ответ поддержки SmetaPro:</b>\n\n{e(message.text)}")
        else:
            await bot.send_message(user_tg, "👨‍💻 <b>Ответ поддержки SmetaPro:</b>")
            await bot.copy_message(user_tg, message.chat.id, message.message_id)
        await message.answer("✅ Ответ отправлен пользователю.")
    except Exception:
        await message.answer("⚠️ Не удалось доставить ответ — возможно, пользователь остановил бота.")


# ---------------------------------------------------------------- пользователь

@router.message(F.text == BTN_SUPPORT)
async def support_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Support.chat)
    contact = get_settings().support_telegram
    extra = f"\n\nТакже можно написать напрямую: {e(contact)}" if contact else ""
    await message.answer("👨‍💻 <b>Техническая поддержка SmetaPro</b>\n\n"
                         "Опишите вопрос или проблему — можно текстом, скриншотом или файлом. "
                         f"Мы ответим здесь же, в боте.{extra}", reply_markup=rk([SUPPORT_DONE], [HOME]))


@router.message(Support.chat, F.text == SUPPORT_DONE)
async def support_done(message: Message, state: FSMContext):
    from app.bot.handlers.start import show_home
    await show_home(message, state, "Спасибо! Если появятся вопросы — мы на связи 👨‍💻")


@router.message(Support.chat, ~F.text.startswith("/"))
async def support_message(message: Message, bot: Bot):
    def work(ctx):
        ent = ctx.enterprise.name if ctx.enterprise else "без предприятия"
        return _admin_ids(ctx.db), ent
    res = await call(message, work, need_enterprise=False)
    if not res:
        return
    admins, ent = res
    u = message.from_user
    head = (f"🆘 <b>Поддержка</b> · {e(u.full_name)}{' @' + e(u.username) if u.username else ''}\n"
            f"Предприятие: {e(ent)} · #u{u.id}\n<i>Ответьте на это сообщение (reply) — ответ уйдёт пользователю.</i>")
    delivered = 0
    for chat_id in admins:
        try:
            if message.text:
                await bot.send_message(chat_id, f"{head}\n\n{e(message.text)}")
            else:
                caption = f"{head}\n\n{e(message.caption or '')}"[:1000]
                await bot.copy_message(chat_id, message.chat.id, message.message_id, caption=caption)
            delivered += 1
        except Exception:
            pass
    if delivered:
        await message.answer("📨 Сообщение отправлено в поддержку. Ответ придёт сюда. Можно дописать ещё.")
    else:
        await message.answer("⚠️ Сейчас не удалось связаться с поддержкой. Попробуйте позже.")
