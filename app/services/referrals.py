"""Реферальная программа (ТЗ, раздел 15).

Цикл накопления = все рефералы пользователя в статусе qualified.
Скидка = 10 п.п. за каждого, максимум 50 %. Скидка одноразовая: при подтверждённой оплате
со скидкой все засчитанные в цикле рефералы переходят в used — цикл сброшен, повторно
они не участвуют.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Conflict, NotFound
from app.models import PaymentRequest, PaymentStatus, Referral, ReferralStatus, User, utcnow
from app.services.audit import log, notify

STEP = 10
MAX_DISCOUNT = 50


def discount_for(count: int) -> int:
    return min(MAX_DISCOUNT, STEP * max(0, count))


def cycle(db: Session, user_id: int) -> list[Referral]:
    return list(db.scalars(select(Referral).where(Referral.referrer_id == user_id,
                                                  Referral.status == ReferralStatus.qualified)
                           .order_by(Referral.qualified_at)))


def available_discount(db: Session, user_id: int) -> tuple[int, list[int]]:
    """(процент, id рефералов цикла). При использовании скидки сбрасывается весь цикл,
    включая приглашения сверх 5-го (ТЗ 15.3: «текущий цикл накопления сбрасывается»)."""
    refs = cycle(db, user_id)
    return discount_for(len(refs)), [r.id for r in refs]


def links(user: User) -> dict:
    s = get_settings()
    return {
        "telegram": f"https://t.me/{s.bot_username}?start=ref_{user.referral_code}",
        "web": f"{s.base_url.rstrip('/')}/r/{user.referral_code}",
    }


def summary(db: Session, user: User) -> dict:
    discount, ids = available_discount(db, user.id)
    history = list(db.scalars(select(Referral).where(Referral.referrer_id == user.id)
                              .order_by(Referral.created_at.desc())))
    return {
        "links": links(user),
        "qualified_in_cycle": len(ids),
        "discount": discount,
        "total_successful": sum(1 for r in history if r.status in (ReferralStatus.qualified, ReferralStatus.used)),
        "history": history,
        "max_discount": MAX_DISCOUNT,
    }


def on_payment_approved(db: Session, payment: PaymentRequest) -> None:
    """Вызывается внутри транзакции подтверждения оплаты."""
    now = utcnow()
    # 1) Списание скидки пригласившего, если она была применена к этой оплате
    if payment.referral_ids:
        for ref in db.scalars(select(Referral).where(Referral.id.in_(payment.referral_ids))):
            if ref.status == ReferralStatus.qualified:
                ref.status = ReferralStatus.used
                ref.used_in_payment_id = payment.id
                ref.used_at = now
        log(db, "referral.discount_used", actor_id=payment.user_id, enterprise_id=payment.enterprise_id,
            entity="payment", entity_id=payment.id, percent=payment.discount_percent)
        notify(db, payment.user_id, f"🎁 Реферальная скидка {payment.discount_percent}% использована. "
                                    "Цикл накопления начат заново — приглашайте новых клиентов.",
               dedup_key=f"ref-used-{payment.id}")

    # 2) Засчитать приглашение, если это первая подтверждённая оплата приглашённого предприятия
    earlier = db.scalar(select(PaymentRequest.id).where(
        PaymentRequest.enterprise_id == payment.enterprise_id,
        PaymentRequest.status == PaymentStatus.approved,
        PaymentRequest.id != payment.id))
    if earlier:
        return  # продление/повторная оплата бонус не создаёт
    ref = db.scalar(select(Referral).where(Referral.referred_enterprise_id == payment.enterprise_id))
    if ref is None:
        ent_owner_ref = db.scalar(select(Referral).where(Referral.referred_user_id == payment.enterprise.owner_id,
                                                         Referral.referred_enterprise_id.is_(None)))
        ref = ent_owner_ref
        if ref:
            ref.referred_enterprise_id = payment.enterprise_id
    if ref is None or ref.status != ReferralStatus.registered:
        return
    if ref.referrer_id == payment.enterprise.owner_id:
        return  # самоприглашение
    ref.status = ReferralStatus.qualified
    ref.first_payment_id = payment.id
    ref.qualified_at = now
    db.flush()
    discount, _ = available_discount(db, ref.referrer_id)
    log(db, "referral.qualified", actor_id=None, entity="referral", entity_id=ref.id,
        referrer_id=ref.referrer_id, payment_id=payment.id)
    notify(db, ref.referrer_id,
           f"🎉 Приглашённое предприятие оплатило подписку! Ваша скидка на следующую покупку: {discount}%.",
           dedup_key=f"ref-q-{ref.id}")


def on_payment_refunded(db: Session, payment: PaymentRequest, admin_id: int) -> None:
    for ref in db.scalars(select(Referral).where(Referral.first_payment_id == payment.id,
                                                 Referral.status == ReferralStatus.qualified)):
        ref.status = ReferralStatus.annulled
        ref.note = f"Возврат платежа {payment.uid}"
        log(db, "referral.annulled", actor_id=admin_id, entity="referral", entity_id=ref.id, reason="refund")


def annul(db: Session, admin: User, referral_id: int, reason: str) -> Referral:
    ref = db.get(Referral, referral_id)
    if not ref:
        raise NotFound("Реферал не найден.")
    if ref.status in (ReferralStatus.used, ReferralStatus.annulled):
        raise Conflict("Начисление уже использовано или аннулировано.")
    ref.status = ReferralStatus.annulled
    ref.note = reason
    log(db, "admin.referral.annul", actor_id=admin.id, entity="referral", entity_id=ref.id, reason=reason)
    notify(db, ref.referrer_id, f"Реферальное начисление аннулировано администратором. Причина: {reason}")
    return ref
