"""CRYPTO PAY: заготовка интеграции (ТЗ 14.2 — сценарий согласуется после получения API-данных).

Реализовано по публичной схеме Crypto Pay API (@CryptoBot): создание инвойса и проверка подписи
вебхука (HMAC-SHA256 тела запроса ключом SHA256(token)). Оплата подтверждается только
по webhook `invoice_paid` с корректной подписью, повторная доставка безопасна (идемпотентный approve).
Способ оплаты CRYPTO выключен по умолчанию — включается в админ-панели после настройки токена.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Conflict, DomainError
from app.models import PaymentRequest, PaymentStatus
from app.services import payments
from app.services.accounts import Ctx

log = logging.getLogger("crypto_pay")


def enabled() -> bool:
    return bool(get_settings().crypto_pay_api_token)


def create_invoice(ctx: Ctx, pr: PaymentRequest) -> str:
    """Создать инвойс и вернуть ссылку на оплату. Заявка переходит в статус «на проверке»."""
    s = get_settings()
    if not enabled():
        raise DomainError("Оплата CRYPTO PAY пока не настроена.")
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{s.crypto_pay_api_url}/createInvoice",
                   headers={"Crypto-Pay-API-Token": s.crypto_pay_api_token},
                   json={"currency_type": "fiat", "fiat": "UZS", "amount": str(pr.amount),
                         "description": f"Furniture Estimate {pr.plan_code} ({pr.uid})", "payload": pr.uid})
    data = r.json()
    if not data.get("ok"):
        log.warning("createInvoice failed: %s", data.get("error"))
        raise DomainError("Не удалось создать счёт CRYPTO PAY. Попробуйте другой способ оплаты.")
    inv = data["result"]
    pr.external_id = str(inv["invoice_id"])
    pr.status = PaymentStatus.pending
    return inv.get("bot_invoice_url") or inv.get("pay_url")


def verify_signature(body: bytes, signature: str | None) -> bool:
    token = get_settings().crypto_pay_api_token
    if not token or not signature:
        return False
    secret = hashlib.sha256(token.encode()).digest()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_update(db: Session, update: dict) -> None:
    if update.get("update_type") != "invoice_paid":
        return
    inv = update.get("payload") or {}
    pr = db.scalar(select(PaymentRequest).where(PaymentRequest.external_id == str(inv.get("invoice_id"))))
    if not pr or inv.get("status") != "paid" or inv.get("payload") != pr.uid:
        log.warning("crypto update for unknown invoice %s", inv.get("invoice_id"))
        return
    try:
        payments.approve(db, None, pr.id, source="crypto_pay")
    except Conflict:
        pass  # повторная доставка вебхука
