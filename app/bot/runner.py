"""Запуск Telegram-бота. Два режима (переменная BOT_MODE):

  polling  — отдельный процесс: python -m app.bot.runner (локально, docker-compose).
  webhook  — бот работает внутри веб-сервиса (uvicorn app.main:app): при старте регистрируется
             webhook https://BASE_URL/telegram/webhook, там же запускаются фоновые задачи
             (рассылка уведомлений, напоминания о подписке). Отдельный процесс не нужен — так удобно
             на Render и других PaaS. Запускать один экземпляр веб-сервиса.
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, Update
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot.core import MENU_BUTTONS
from app.bot.handlers import admin, business, calc, expenses, start, stock, support
from app.config import get_settings
from app.db import session_scope
from app.models import Notification, ProcessedUpdate, User, utcnow
from app.services import jobs
from app.services.seed import ensure_seed

log = logging.getLogger("bot")


class DedupMiddleware(BaseMiddleware):
    """Каждый update_id обрабатывается один раз — даже после перезапуска или повторной доставки webhook."""

    async def __call__(self, handler, event: Update, data):
        def mark() -> bool:
            try:
                with session_scope() as db:
                    db.add(ProcessedUpdate(update_id=event.update_id))
                return True
            except IntegrityError:
                return False
        if not await asyncio.to_thread(mark):
            log.info("duplicate update %s skipped", event.update_id)
            return None
        try:
            return await handler(event, data)
        finally:
            kick()  # действие могло поставить уведомления в очередь — доставить сразу


class MenuResetMiddleware(BaseMiddleware):
    """Нажатие кнопки главного меню прерывает любой незавершённый диалог."""

    async def __call__(self, handler, event: Message, data):
        if event.text in MENU_BUTTONS:
            state: FSMContext | None = data.get("state")
            if state:
                await state.clear()
            data["raw_state"] = None  # фильтры состояний читают уже вычисленное значение
        return await handler(event, data)


_dispatcher: Dispatcher | None = None


def build() -> tuple[Bot, Dispatcher]:
    """Новый экземпляр Bot и единый Dispatcher (роутеры — модульные синглтоны, подключаются один раз)."""
    global _dispatcher
    s = get_settings()
    if not s.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")
    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if _dispatcher is None:
        _dispatcher = _make_dispatcher()
    return bot, _dispatcher


def _make_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(DedupMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware())
    dp.include_routers(support.router, start.router, admin.router, calc.router, stock.router, expenses.router,
                       business.router)

    @dp.errors()
    async def on_error(event):
        log.exception("bot handler error", exc_info=event.exception)
        upd = event.update
        target = upd.message or (upd.callback_query.message if upd.callback_query else None)
        if target:
            try:
                await target.answer("Что-то пошло не так. Попробуйте ещё раз или отправьте /menu.")
            except Exception:
                pass
        return True
    return dp


# ---------------------------------------------------------------- фоновые задачи

async def deliver_notifications(bot: Bot) -> int:
    def fetch():
        with session_scope() as db:
            rows = db.execute(select(Notification.id, Notification.text, User.telegram_id)
                              .join(User, User.id == Notification.user_id)
                              .where(Notification.sent_at.is_(None), Notification.failed.is_(False))
                              .order_by(Notification.id).limit(25)).all()
            return [tuple(r) for r in rows]

    def mark(nid: int, ok: bool, permanent: bool = False):
        with session_scope() as db:
            n = db.get(Notification, nid)
            if ok:
                n.sent_at = utcnow()
            else:
                n.attempts += 1
                n.failed = permanent or n.attempts >= 5

    sent = 0
    for nid, text, tg_id in await asyncio.to_thread(fetch):
        if not tg_id:
            await asyncio.to_thread(mark, nid, False, True)
            continue
        try:
            await bot.send_message(tg_id, text, disable_web_page_preview=True)
            await asyncio.to_thread(mark, nid, True)
            sent += 1
        except TelegramForbiddenError:
            await asyncio.to_thread(mark, nid, False, True)  # пользователь заблокировал бота
        except Exception:
            log.exception("notification %s failed", nid)
            await asyncio.to_thread(mark, nid, False)
        await asyncio.sleep(0.05)  # ограничения Telegram на частоту
    return sent


_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def kick() -> None:
    """Разбудить фоновую задачу: появились новые уведомления. Можно вызывать из любого потока."""
    if _loop and _wake and not _loop.is_closed():
        _loop.call_soon_threadsafe(_wake.set)


async def background(bot: Bot):
    """Уведомления доставляются по сигналу kick() (после действий пользователей), периодические
    задачи — раз в JOBS_INTERVAL_SECONDS. Между ними к БД нет обращений — бесплатная БД может спать."""
    global _wake, _loop
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    interval = max(30, get_settings().jobs_interval_seconds)
    last_jobs = 0.0

    def run_jobs():
        with session_scope() as db:
            jobs.subscription_reminders(db)
            jobs.cleanup(db)

    while True:
        try:
            if time.monotonic() - last_jobs >= interval:
                await asyncio.to_thread(run_jobs)
                last_jobs = time.monotonic()
            await deliver_notifications(bot)
        except Exception:
            log.exception("background loop error")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=interval)
            await asyncio.sleep(1)  # дать завершиться транзакции, которая создала уведомление
        except asyncio.TimeoutError:
            pass
        _wake.clear()


# ---------------------------------------------------------------- webhook

_bot: Bot | None = None
_dp: Dispatcher | None = None


_bg_task: asyncio.Task | None = None


async def feed_update(payload: dict) -> None:
    global _bot, _dp
    if _dp is None:
        _bot, _dp = build()
    await _dp.feed_update(_bot, Update.model_validate(payload, context={"bot": _bot}))


async def start_embedded() -> None:
    """Режим webhook внутри веб-сервиса: регистрация webhook и фоновые задачи."""
    global _bot, _dp, _bg_task
    s = get_settings()
    _bot, _dp = build()
    url = f"{s.base_url.rstrip('/')}/telegram/webhook"
    try:
        await _bot.set_webhook(url, secret_token=s.effective_webhook_secret, drop_pending_updates=False,
                               allowed_updates=_dp.resolve_used_update_types())
        log.info("Telegram webhook set: %s", url)
    except Exception:
        log.exception("Не удалось установить webhook — проверьте BOT_TOKEN и BASE_URL")
    _bg_task = asyncio.create_task(background(_bot))


async def stop_embedded() -> None:
    if _bg_task:
        _bg_task.cancel()
    if _bot:
        await _bot.session.close()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    with session_scope() as db:
        ensure_seed(db)
    if s.bot_mode == "webhook":
        raise SystemExit("BOT_MODE=webhook: бот работает внутри веб-сервиса (uvicorn app.main:app), "
                         "отдельный процесс не нужен.")
    bot, dp = build()
    bg = asyncio.create_task(background(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        bg.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
