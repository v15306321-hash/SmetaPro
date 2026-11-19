"""Периодические задачи: напоминания и окончание подписок, очистка устаревших данных.
Идемпотентны (dedup_key), поэтому безопасны при перезапуске и параллельном запуске."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.fmt import date_str
from app.models import (Enterprise, LoginRequest, ProcessedUpdate, Subscription, utcnow)
from app.services import tariffs
from app.services.audit import log, notify


def subscription_reminders(db: Session) -> int:
    now = utcnow()
    sent = 0
    subs = db.scalars(select(Subscription).where(Subscription.superseded_at.is_(None),
                                                 Subscription.ends_at > now - timedelta(days=2),
                                                 Subscription.starts_at <= now)).all()
    for sub in subs:
        ent = db.get(Enterprise, sub.enterprise_id)
        # есть ли продолжение (оплаченный следующий период)?
        nxt = db.scalar(select(Subscription.id).where(Subscription.enterprise_id == sub.enterprise_id,
                                                      Subscription.superseded_at.is_(None),
                                                      Subscription.ends_at > sub.ends_at))
        if nxt:
            continue
        left = sub.ends_at - now
        if timedelta(0) < left <= timedelta(days=3):
            key = f"sub-rem-{sub.id}-{'1' if left <= timedelta(days=1) else '3'}"
            notify(db, ent.owner_id, f"⏳ Подписка {sub.plan_code} для «{ent.name}» заканчивается "
                                     f"{date_str(sub.ends_at)}. Продлите её в разделе «Подписка», чтобы не "
                                     "прерывать работу.", dedup_key=key)
            sent += 1
        elif left <= timedelta(0) and not tariffs.current_subscription(db, sub.enterprise_id):
            notify(db, ent.owner_id, f"🔒 Подписка {sub.plan_code} для «{ent.name}» истекла. Платные функции "
                                     "заблокированы, все данные сохранены. Продлите подписку, чтобы продолжить.",
                   dedup_key=f"sub-exp-{sub.id}")
            log(db, "subscription.expired", enterprise_id=ent.id, entity="subscription", entity_id=sub.id)
            sent += 1
    return sent


def cleanup(db: Session) -> None:
    now = utcnow()
    db.execute(delete(LoginRequest).where(LoginRequest.expires_at < now - timedelta(days=1)))
    db.execute(delete(ProcessedUpdate).where(ProcessedUpdate.created_at < now - timedelta(days=3)))
