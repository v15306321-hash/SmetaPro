"""Вебхуки: Telegram (production-режим бота) и CRYPTO PAY."""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import session_scope
from app.services import crypto_pay

router = APIRouter(include_in_schema=False)
log = logging.getLogger("webhooks")


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    s = get_settings()
    if not s.webhook_secret or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", s.webhook_secret):
        return JSONResponse({"ok": False}, 403)
    from app.bot.runner import feed_update
    await feed_update(await request.json())
    return {"ok": True}


@router.post("/payments/crypto/webhook")
async def crypto_webhook(request: Request, crypto_pay_api_signature: str | None = Header(default=None)):
    body = await request.body()
    if not crypto_pay.verify_signature(body, crypto_pay_api_signature):
        return JSONResponse({"ok": False}, 403)
    try:
        with session_scope() as db:
            crypto_pay.handle_update(db, json.loads(body))
    except Exception:
        log.exception("crypto webhook failed")
        return JSONResponse({"ok": False}, 500)
    return {"ok": True}
