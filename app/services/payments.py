"""Оплата подписок с ручным подтверждением администратором (ТЗ, раздел 14).

Идемпотентность: смена статуса выполняется условным UPDATE ... WHERE status = <ожидаемый>,
поэтому повторное нажатие «Подтвердить» (в боте, на сайте, двумя админами одновременно)
не продлит подписку дважды.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound, ValidationError
from app.core.fmt import money, money_round
from app.core.security import short_code
from app.models import (PaymentMethod, PaymentRequest, PaymentStatus, Plan, User, utcnow)
from app.services import referrals, tariffs
from app.services.accounts import Ctx
from app.services.audit import log, notify

OPEN_STATUSES = (PaymentStatus.awaiting_receipt, PaymentStatus.pending)

STATUS_TITLES = {
    PaymentStatus.awaiting_receipt: "Ожидает чек",
    PaymentStatus.pending: "На проверке",
    PaymentStatus.approved: "Подтверждена",
    PaymentStatus.rejected: "Отклонена",
    PaymentStatus.cancelled: "Отменена",
}


def list_methods(db: Session, only_active: bool = True) -> list[PaymentMethod]:
    q = select(PaymentMethod).order_by(PaymentMethod.sort)
    if only_active:
        q = q.where(PaymentMethod.is_active.is_(True))
    return list(db.scalars(q))


def quote(db: Session, user: User, plan: Plan) -> dict:
    discount, ref_ids = referrals.available_discount(db, user.id)
    amount = money_round(plan.price * (Decimal(100 - discount) / 100))
    return dict(base=plan.price, discount=discount, amount=amount, referral_ids=ref_ids)


def create_request(ctx: Ctx, plan_code: str, method_code: str) -> PaymentRequest:
    ctx.require("billing.manage")
    db = ctx.db
    plan = tariffs.get_plan(db, plan_code)
    if not plan or not plan.is_active:
        raise ValidationError("Тариф недоступен.")
    method = db.get(PaymentMethod, method_code)
    if not method or not method.is_active:
        raise ValidationError("Способ оплаты недоступен.")
    if db.scalar(select(PaymentRequest.id).where(PaymentRequest.enterprise_id == ctx.eid,
                                                 PaymentRequest.status == PaymentStatus.pending)):
        raise Conflict("У предприятия уже есть заявка на проверке. Дождитесь решения администратора.")
    # незавершённые заявки без чека заменяются новой
    for old in db.scalars(select(PaymentRequest).where(PaymentRequest.enterprise_id == ctx.eid,
                                                       PaymentRequest.status == PaymentStatus.awaiting_receipt)):
        old.status = PaymentStatus.cancelled
    q = quote(db, ctx.user, plan)
    pr = PaymentRequest(uid=f"PAY-{short_code(8)}", enterprise_id=ctx.eid, user_id=ctx.user.id,
                        plan_code=plan.code, method_code=method.code, base_amount=q["base"],
                        discount_percent=q["discount"], amount=q["amount"],
                        referral_ids=q["referral_ids"] or None)
    db.add(pr)
    db.flush()
    log(db, "payment.created", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="payment", entity_id=pr.uid,
        plan=plan.code, method=method.code, amount=str(pr.amount), discount=pr.discount_percent)
    return pr


def get_for_ctx(ctx: Ctx, payment_id: int) -> PaymentRequest:
    pr = ctx.db.get(PaymentRequest, payment_id)
    if not pr or pr.enterprise_id != ctx.eid:
        raise NotFound("Заявка не найдена.")
    return pr


def attach_receipt(ctx: Ctx, payment_id: int, *, file_id: str | None = None, path: str | None = None) -> PaymentRequest:
    ctx.require("billing.manage")
    pr = get_for_ctx(ctx, payment_id)
    if pr.status != PaymentStatus.awaiting_receipt:
        raise Conflict("Чек к этой заявке уже приложен или заявка закрыта.")
    if not file_id and not path:
        raise ValidationError("Приложите фото или файл чека.")
    pr.receipt_file_id = file_id
    pr.receipt_path = path
    pr.status = PaymentStatus.pending
    log(ctx.db, "payment.receipt", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="payment", entity_id=pr.uid)
    for admin in ctx.db.scalars(select(User).where(User.is_superadmin.is_(True))):
        notify(ctx.db, admin.id,
               f"💳 Новая заявка {pr.uid}\nПредприятие: {pr.enterprise.name}\nТариф: {pr.plan_code}\n"
               f"Сумма: {money(pr.amount)} ({pr.method.title})\nОткройте /admin для проверки.",
               dedup_key=f"pay-new-{pr.id}-{admin.id}")
    notify(ctx.db, ctx.user.id, f"🧾 Заявка {pr.uid} отправлена на проверку. Мы сообщим о решении.")
    return pr


def cancel(ctx: Ctx, payment_id: int) -> None:
    ctx.require("billing.manage")
    pr = get_for_ctx(ctx, payment_id)
    res = ctx.db.execute(update(PaymentRequest).where(PaymentRequest.id == pr.id,
                                                      PaymentRequest.status == PaymentStatus.awaiting_receipt)
                         .values(status=PaymentStatus.cancelled))
    if res.rowcount != 1:
        raise Conflict("Заявку нельзя отменить.")


def approve(db: Session, admin: User | None, payment_id: int, *, source: str = "admin") -> PaymentRequest:
    """Подтверждение оплаты. admin=None — автоматическое подтверждение (CRYPTO PAY webhook)."""
    res = db.execute(update(PaymentRequest)
                     .where(PaymentRequest.id == payment_id, PaymentRequest.status == PaymentStatus.pending)
                     .values(status=PaymentStatus.approved, reviewed_by_id=admin.id if admin else None,
                             reviewed_at=utcnow()))
    if res.rowcount != 1:
        pr = db.get(PaymentRequest, payment_id)
        if not pr:
            raise NotFound("Заявка не найдена.")
        raise Conflict(f"Заявка {pr.uid} уже обработана ({STATUS_TITLES[pr.status]}).")
    pr = db.get(PaymentRequest, payment_id)
    db.refresh(pr)
    sub = tariffs.activate(db, pr.enterprise_id, pr.plan_code, pr.id)
    referrals.on_payment_approved(db, pr)
    log(db, "payment.approved", actor_id=admin.id if admin else None, enterprise_id=pr.enterprise_id,
        entity="payment", entity_id=pr.uid, amount=str(pr.amount), plan=pr.plan_code, source=source,
        subscription_id=sub.id)
    from app.core.fmt import date_str
    notify(db, pr.user_id,
           f"✅ Оплата {pr.uid} подтверждена. Тариф {pr.plan_code} активен "
           f"с {date_str(sub.starts_at)} по {date_str(sub.ends_at)}.",
           dedup_key=f"pay-ok-{pr.id}")
    return pr


def reject(db: Session, admin: User, payment_id: int, reason: str | None) -> PaymentRequest:
    reason = (reason or "").strip()[:500] or None
    res = db.execute(update(PaymentRequest)
                     .where(PaymentRequest.id == payment_id, PaymentRequest.status.in_(OPEN_STATUSES))
                     .values(status=PaymentStatus.rejected, reject_reason=reason,
                             reviewed_by_id=admin.id, reviewed_at=utcnow()))
    pr = db.get(PaymentRequest, payment_id)
    if not pr:
        raise NotFound("Заявка не найдена.")
    if res.rowcount != 1:
        raise Conflict(f"Заявка {pr.uid} уже обработана ({STATUS_TITLES[pr.status]}).")
    db.refresh(pr)
    log(db, "payment.rejected", actor_id=admin.id, enterprise_id=pr.enterprise_id, entity="payment",
        entity_id=pr.uid, reason=reason)
    notify(db, pr.user_id, f"❌ Заявка {pr.uid} отклонена." + (f"\nПричина: {reason}" if reason else "")
           + "\nЕсли это ошибка — свяжитесь с поддержкой.", dedup_key=f"pay-rej-{pr.id}")
    return pr


def refund(db: Session, admin: User, payment_id: int, reason: str) -> PaymentRequest:
    """Отметка возврата: подписка этой оплаты прекращается, неиспользованный реферальный бонус аннулируется."""
    from app.models import Subscription
    pr = db.get(PaymentRequest, payment_id)
    if not pr or pr.status != PaymentStatus.approved:
        raise Conflict("Возврат возможен только для подтверждённой оплаты.")
    if pr.refunded:
        raise Conflict("Возврат уже отмечен.")
    pr.refunded = True
    now = utcnow()
    for sub in db.scalars(select(Subscription).where(Subscription.payment_id == pr.id,
                                                     Subscription.superseded_at.is_(None))):
        sub.superseded_at = now
    referrals.on_payment_refunded(db, pr, admin.id)
    log(db, "payment.refunded", actor_id=admin.id, enterprise_id=pr.enterprise_id, entity="payment",
        entity_id=pr.uid, reason=reason)
    return pr


def history(db: Session, enterprise_id: int) -> list[PaymentRequest]:
    return list(db.scalars(select(PaymentRequest).where(PaymentRequest.enterprise_id == enterprise_id)
                           .order_by(PaymentRequest.created_at.desc())))


def update_method(db: Session, admin: User, code: str, *, title: str | None = None, details: str | None = None,
                  holder: str | None = None, is_active: bool | None = None) -> PaymentMethod:
    m = db.get(PaymentMethod, code)
    if not m:
        raise NotFound("Способ оплаты не найден.")
    before = dict(details=m.details, holder=m.holder, is_active=m.is_active, title=m.title)
    if title is not None:
        m.title = title.strip()[:64]
    if details is not None:
        m.details = details.strip()[:300]
    if holder is not None:
        m.holder = holder.strip()[:120]
    if is_active is not None:
        m.is_active = is_active
    log(db, "admin.payment_method.update", actor_id=admin.id, entity="payment_method", entity_id=code,
        before=before, after=dict(details=m.details, holder=m.holder, is_active=m.is_active, title=m.title))
    return m
