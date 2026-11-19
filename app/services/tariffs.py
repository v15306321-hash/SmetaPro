"""Тарифы, подписки, функции и лимиты. Единственное место проверки доступности функций."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import FeatureUnavailable, LimitReached, SubscriptionRequired
from app.core.fmt import date_str
from app.models import Plan, Subscription, utcnow

PERIOD_DAYS = 30

FEATURE_TITLES: dict[str, str] = {
    "custom_markups": "Индивидуальные наценки",
    "analytics": "Финансовая аналитика",
    "pdf_logo": "Логотип в PDF-смете",
    "expenses": "Производственные затраты",
    "waste": "Учёт отходов и расхода",
    "order_statuses": "Статусы заказов",
}

# Значения из ТЗ, раздел 4. Используются только для начального заполнения БД;
# далее цены/лимиты/функции редактируются администратором.
DEFAULT_PLANS = [
    dict(code="START", title="START", subtitle="Небольшая мастерская", price=Decimal("149000"),
         estimates_limit=15, materials_limit=80, users_limit=1, sort=1,
         features=dict(custom_markups=True, analytics=False, pdf_logo=False, expenses=False,
                       waste=False, order_statuses=False)),
    dict(code="BUSINESS", title="BUSINESS", subtitle="Перспективный бизнес", price=Decimal("349000"),
         estimates_limit=40, materials_limit=250, users_limit=2, sort=2,
         features=dict(custom_markups=True, analytics=True, pdf_logo=True, expenses=True,
                       waste=False, order_statuses=False)),
    dict(code="PRO", title="PRO", subtitle="Для крупных производителей", price=Decimal("749000"),
         estimates_limit=100, materials_limit=500, users_limit=5, sort=3,
         features=dict(custom_markups=True, analytics=True, pdf_logo=True, expenses=True,
                       waste=True, order_statuses=True)),
    dict(code="ENTERPRISE", title="ENTERPRISE", subtitle="Для заводов и фабрик", price=Decimal("1490000"),
         estimates_limit=300, materials_limit=2000, users_limit=10, sort=4,
         features=dict(custom_markups=True, analytics=True, pdf_logo=True, expenses=True,
                       waste=True, order_statuses=True)),
]


def list_plans(db: Session, only_active: bool = True) -> list[Plan]:
    q = select(Plan).order_by(Plan.sort)
    if only_active:
        q = q.where(Plan.is_active.is_(True))
    return list(db.scalars(q))


def get_plan(db: Session, code: str) -> Plan | None:
    return db.get(Plan, code)


def current_subscription(db: Session, enterprise_id: int, now: datetime | None = None) -> Subscription | None:
    now = now or utcnow()
    return db.scalars(
        select(Subscription)
        .where(Subscription.enterprise_id == enterprise_id, Subscription.starts_at <= now,
               Subscription.ends_at > now, Subscription.superseded_at.is_(None))
        .order_by(Subscription.starts_at.desc())
    ).first()


def queued_subscriptions(db: Session, enterprise_id: int, now: datetime | None = None) -> list[Subscription]:
    now = now or utcnow()
    return list(db.scalars(
        select(Subscription)
        .where(Subscription.enterprise_id == enterprise_id, Subscription.starts_at > now,
               Subscription.superseded_at.is_(None))
        .order_by(Subscription.starts_at)))


def last_subscription(db: Session, enterprise_id: int) -> Subscription | None:
    return db.scalars(select(Subscription).where(Subscription.enterprise_id == enterprise_id)
                      .order_by(Subscription.ends_at.desc())).first()


def subscription_history(db: Session, enterprise_id: int) -> list[Subscription]:
    return list(db.scalars(select(Subscription).where(Subscription.enterprise_id == enterprise_id)
                           .order_by(Subscription.starts_at.desc())))


def activate(db: Session, enterprise_id: int, plan_code: str, payment_id: int | None,
             now: datetime | None = None) -> Subscription:
    """Активация/продление после подтверждённой оплаты.

    • Тот же тариф при активной подписке — продление: новый 30-дневный период начинается
      после окончания текущего (оплаченные дни не теряются); лимит смет обновится в новом периоде.
    • Другой тариф — смена: новый тариф действует сразу, 30 дней с момента подтверждения,
      лимиты и права нового тарифа применяются немедленно, счётчик смет начинается заново.
    """
    now = now or utcnow()
    cur = current_subscription(db, enterprise_id, now)
    if cur and cur.plan_code == plan_code:
        chain = [cur] + queued_subscriptions(db, enterprise_id, now)
        starts = max(s.ends_at for s in chain)
    else:
        if cur:
            cur.superseded_at = now
        for q in queued_subscriptions(db, enterprise_id, now):
            q.superseded_at = now
        starts = now
    sub = Subscription(enterprise_id=enterprise_id, plan_code=plan_code, starts_at=starts,
                       ends_at=starts + timedelta(days=PERIOD_DAYS), payment_id=payment_id)
    db.add(sub)
    db.flush()
    return sub


# ---------------------------------------------------------------- проверки

def require_active(db: Session, enterprise_id: int) -> Subscription:
    sub = current_subscription(db, enterprise_id)
    if not sub:
        last = last_subscription(db, enterprise_id)
        if last:
            raise SubscriptionRequired(
                f"Подписка истекла {date_str(last.ends_at)}. Данные сохранены — продлите подписку, "
                "чтобы продолжить работу.")
        raise SubscriptionRequired("Для работы с этой функцией нужна активная подписка. Выберите тариф в разделе «Подписка».")
    return sub


def has_feature(db: Session, enterprise_id: int, feature: str) -> bool:
    sub = current_subscription(db, enterprise_id)
    return bool(sub and sub.plan.features.get(feature))


def min_plan_for(db: Session, feature: str) -> Plan | None:
    for p in list_plans(db):
        if p.features.get(feature):
            return p
    return None


def require_feature(db: Session, enterprise_id: int, feature: str) -> Subscription:
    sub = require_active(db, enterprise_id)
    if not sub.plan.features.get(feature):
        mp = min_plan_for(db, feature)
        title = FEATURE_TITLES.get(feature, feature)
        hint = f" Доступно с тарифа {mp.title}." if mp else ""
        raise FeatureUnavailable(f"«{title}» недоступно на тарифе {sub.plan.title}.{hint}",
                                 feature=feature, min_plan=mp.code if mp else None)
    return sub


def consume_estimate(db: Session, sub: Subscription) -> None:
    """Атомарно списать одну генерацию сметы (защита от гонок при параллельных запросах)."""
    limit = sub.plan.estimates_limit
    res = db.execute(update(Subscription)
                     .where(Subscription.id == sub.id, Subscription.estimates_used < limit)
                     .values(estimates_used=Subscription.estimates_used + 1))
    if res.rowcount != 1:
        raise LimitReached(
            f"Лимит генераций смет исчерпан ({limit} за период). Он обновится с новым оплаченным периодом "
            "или при переходе на тариф выше.", limit=limit)
    db.refresh(sub)


def usage(db: Session, enterprise_id: int) -> dict:
    """Сводка лимитов для кабинета/бота."""
    from app.models import Material, Membership
    sub = current_subscription(db, enterprise_id)
    materials = db.query(Material).filter_by(enterprise_id=enterprise_id, is_archived=False).count()
    users = db.query(Membership).filter_by(enterprise_id=enterprise_id, is_active=True).count()
    if not sub:
        return dict(subscription=None, materials=materials, users=users)
    p = sub.plan
    return dict(subscription=sub, plan=p,
                estimates_used=sub.estimates_used, estimates_limit=p.estimates_limit,
                materials=materials, materials_limit=p.materials_limit,
                users=users, users_limit=p.users_limit,
                days_left=max(0, (sub.ends_at - utcnow()).days))
