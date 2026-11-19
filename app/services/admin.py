"""Административные операции платформы (ТЗ, раздел 17). Все изменения журналируются."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.fmt import D
from app.models import (AuditLog, Enterprise, Estimate, Order, PaymentRequest, PaymentStatus, Plan, Referral,
                        Subscription, User, utcnow)
from app.services.audit import log
from app.services.tariffs import FEATURE_TITLES


def require_superadmin(user: User | None) -> User:
    if not user or not user.is_superadmin or user.is_blocked:
        raise PermissionDenied("Доступ только для администраторов платформы.")
    return user


def stats(db: Session) -> dict:
    now = utcnow()
    active = db.execute(select(Subscription.plan_code, func.count(func.distinct(Subscription.enterprise_id)))
                        .where(Subscription.starts_at <= now, Subscription.ends_at > now,
                               Subscription.superseded_at.is_(None))
                        .group_by(Subscription.plan_code)).all()
    month_ago = now - timedelta(days=30)
    regs = db.execute(select(func.date(User.created_at), func.count(User.id))
                      .where(User.created_at >= month_ago).group_by(func.date(User.created_at))).all()
    revenue_30 = db.scalar(select(func.coalesce(func.sum(PaymentRequest.amount), 0))
                           .where(PaymentRequest.status == PaymentStatus.approved,
                                  PaymentRequest.refunded.is_(False),
                                  PaymentRequest.reviewed_at >= month_ago))
    return dict(
        users=db.scalar(select(func.count(User.id))),
        enterprises=db.scalar(select(func.count(Enterprise.id))),
        active_by_plan={code: n for code, n in active},
        active_total=sum(n for _, n in active),
        pending_payments=db.scalar(select(func.count(PaymentRequest.id))
                                   .where(PaymentRequest.status == PaymentStatus.pending)),
        revenue_30=D(revenue_30),
        estimates_30=db.scalar(select(func.count(Estimate.id)).where(Estimate.created_at >= month_ago)),
        orders_30=db.scalar(select(func.count(Order.id)).where(Order.created_at >= month_ago)),
        registrations=[(str(d), n) for d, n in regs],
        referrals_qualified=db.scalar(select(func.count(Referral.id)).where(Referral.qualified_at.is_not(None))),
    )


def search_users(db: Session, q: str | None, limit: int = 50) -> list[User]:
    query = select(User)
    if q:
        like = f"%{q.strip()}%"
        conds = [User.full_name.ilike(like), User.telegram_username.ilike(like), User.phone.ilike(like),
                 User.referral_code.ilike(like)]
        if q.strip().isdigit():
            conds.append(User.telegram_id == int(q.strip()))
        query = query.where(or_(*conds))
    return list(db.scalars(query.order_by(User.created_at.desc()).limit(limit)))


def search_enterprises(db: Session, q: str | None, limit: int = 50) -> list[Enterprise]:
    query = select(Enterprise)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(Enterprise.name.ilike(like), Enterprise.phone.ilike(like)))
    return list(db.scalars(query.order_by(Enterprise.created_at.desc()).limit(limit)))


def update_plan(db: Session, admin: User, code: str, *, price=None, estimates_limit=None, materials_limit=None,
                users_limit=None, subtitle=None, is_active=None, features: dict | None = None) -> Plan:
    plan = db.get(Plan, code)
    if not plan:
        raise NotFound("Тариф не найден.")
    before = dict(price=str(plan.price), estimates=plan.estimates_limit, materials=plan.materials_limit,
                  users=plan.users_limit, features=dict(plan.features), is_active=plan.is_active)
    if price is not None:
        p = D(price)
        if p <= 0:
            raise ValidationError("Цена должна быть больше нуля.")
        plan.price = p
    for attr, val in (("estimates_limit", estimates_limit), ("materials_limit", materials_limit),
                      ("users_limit", users_limit)):
        if val is not None:
            v = int(val)
            if v < 1:
                raise ValidationError("Лимит должен быть не меньше 1.")
            setattr(plan, attr, v)
    if subtitle is not None:
        plan.subtitle = subtitle.strip()[:120]
    if is_active is not None:
        plan.is_active = is_active
    if features is not None:
        plan.features = {k: bool(features.get(k)) for k in FEATURE_TITLES}
    log(db, "admin.plan.update", actor_id=admin.id, entity="plan", entity_id=code, before=before,
        after=dict(price=str(plan.price), estimates=plan.estimates_limit, materials=plan.materials_limit,
                   users=plan.users_limit, features=dict(plan.features), is_active=plan.is_active))
    return plan


def audit_entries(db: Session, action: str | None = None, limit: int = 200) -> list[AuditLog]:
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.ilike(f"{action}%"))
    return list(db.scalars(q.order_by(AuditLog.id.desc()).limit(limit)))


def payments(db: Session, status: PaymentStatus | None = None, limit: int = 100) -> list[PaymentRequest]:
    q = select(PaymentRequest)
    if status:
        q = q.where(PaymentRequest.status == status)
    return list(db.scalars(q.order_by(PaymentRequest.created_at.desc()).limit(limit)))


def referrals_list(db: Session, limit: int = 200) -> list[Referral]:
    return list(db.scalars(select(Referral).order_by(Referral.created_at.desc()).limit(limit)))


def grant_subscription(db: Session, admin: User, enterprise_id: int, plan_code: str, reason: str) -> Subscription:
    """Ручная активация (компенсация/тест) — отдельно журналируется."""
    from app.services import tariffs
    if not db.get(Enterprise, enterprise_id):
        raise NotFound("Предприятие не найдено.")
    if not tariffs.get_plan(db, plan_code):
        raise NotFound("Тариф не найден.")
    if not reason.strip():
        raise ValidationError("Укажите причину.")
    sub = tariffs.activate(db, enterprise_id, plan_code, None)
    log(db, "admin.subscription.grant", actor_id=admin.id, enterprise_id=enterprise_id, entity="subscription",
        entity_id=sub.id, plan=plan_code, reason=reason)
    return sub


_ = Decimal
