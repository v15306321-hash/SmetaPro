"""Модель данных Furniture Estimate.

Все данные предприятий содержат enterprise_id; доступ к ним выполняется только
через сервисный слой, который фильтрует запросы по предприятию (tenant isolation).
Время хранится в UTC (naive datetime). Денежные суммы — Numeric(16, 2), валюта UZS.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Enum, ForeignKey, Integer, LargeBinary, Numeric,
    String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


Money = Numeric(16, 2)
Qty = Numeric(14, 3)


class Role(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    master = "master"
    manager = "manager"


class OrderStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    manufactured = "manufactured"
    completed = "completed"


class ItemKind(str, enum.Enum):
    material = "material"
    hardware = "hardware"
    work = "work"


class PaymentStatus(str, enum.Enum):
    awaiting_receipt = "awaiting_receipt"  # заявка создана, чек ещё не приложен
    pending = "pending"                    # чек приложен, ждёт администратора
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class ReferralStatus(str, enum.Enum):
    registered = "registered"   # зарегистрировался по ссылке, оплаты ещё нет
    qualified = "qualified"     # первая платная подписка подтверждена — засчитан в текущий цикл
    used = "used"               # скидка по циклу использована
    annulled = "annulled"       # аннулирован администратором / возврат


# ---------------------------------------------------------------- users

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(8), default="ru")
    referral_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    active_enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id", use_alter=True, name="fk_users_active_enterprise"))
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Enterprise(Base):
    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    address: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(120))
    tax_id: Mapped[str | None] = mapped_column(String(32))  # ИНН (необязательно)
    logo_path: Mapped[str | None] = mapped_column(String(300))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Индивидуальные наценки (доступны во всех тарифах), проценты
    markup_materials: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal("30"))
    markup_hardware: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal("20"))
    markup_works: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=Decimal("0"))
    fixed_addition: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    fixed_addition_label: Mapped[str] = mapped_column(String(120), default="Дополнительные услуги")
    order_seq: Mapped[int] = mapped_column(Integer, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "enterprise_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    # Для роли «администратор» — список прав, выданных владельцем
    permissions: Mapped[list | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship()
    enterprise: Mapped[Enterprise] = relationship()


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=16))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------- tariffs / billing

class Plan(Base):
    """Тариф. Цены, лимиты и функции управляются из админ-панели, а не из кода обработчиков."""
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    subtitle: Mapped[str] = mapped_column(String(120))
    price: Mapped[Decimal] = mapped_column(Money)
    estimates_limit: Mapped[int] = mapped_column(Integer)
    materials_limit: Mapped[int] = mapped_column(Integer)
    users_limit: Mapped[int] = mapped_column(Integer)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    estimates_used: Mapped[int] = mapped_column(Integer, default=0)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment_requests.id"))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime)  # заменена при смене тарифа
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    plan: Mapped[Plan] = relationship()


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(String(300), default="")  # реквизиты (не секрет)
    holder: Mapped[str] = mapped_column(String(120), default="")
    kind: Mapped[str] = mapped_column(String(16), default="manual")  # manual | crypto
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    method_code: Mapped[str] = mapped_column(ForeignKey("payment_methods.code"))
    base_amount: Mapped[Decimal] = mapped_column(Money)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Money)
    referral_ids: Mapped[list | None] = mapped_column(JSON)  # рефералы, давшие скидку
    receipt_file_id: Mapped[str | None] = mapped_column(String(256))  # Telegram file_id
    receipt_path: Mapped[str | None] = mapped_column(String(300))     # файл, загруженный через сайт
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)  # id инвойса CRYPTO PAY
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=20), default=PaymentStatus.awaiting_receipt, index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500))
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    enterprise: Mapped[Enterprise] = relationship()
    user: Mapped[User] = relationship(foreign_keys=[user_id])
    plan: Mapped[Plan] = relationship()
    method: Mapped[PaymentMethod] = relationship()


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    # Одно предприятие засчитывается не более одного раза за всё время
    referred_enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id"), unique=True)
    first_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment_requests.id"))
    used_in_payment_id: Mapped[int | None] = mapped_column(ForeignKey("payment_requests.id"))
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus, native_enum=False, length=16), default=ReferralStatus.registered, index=True)
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    qualified_at: Mapped[datetime | None] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime)

    referrer: Mapped[User] = relationship(foreign_keys=[referrer_id])
    referred_user: Mapped[User] = relationship(foreign_keys=[referred_user_id])
    referred_enterprise: Mapped[Enterprise | None] = relationship()


# ---------------------------------------------------------------- production

class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    kind: Mapped[ItemKind] = mapped_column(Enum(ItemKind, native_enum=False, length=16))  # material | hardware
    category: Mapped[str] = mapped_column(String(80), default="")  # ЛДСП, МДФ, петли, ...
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Money)
    attrs: Mapped[dict | None] = mapped_column(JSON)  # толщина, цвет, артикул, ...
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("enterprise_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200), default="")
    client_name: Mapped[str] = mapped_column(String(200), default="")
    client_phone: Mapped[str | None] = mapped_column(String(32))
    client_address: Mapped[str | None] = mapped_column(String(300))
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=16), default=OrderStatus.new)
    # Наценки зафиксированы в заказе, чтобы изменение настроек не меняло старые расчёты
    markup_materials: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    markup_hardware: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    markup_works: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    fixed_addition: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    # Кэш итогов (пересчитывается сервисом calculator.recalc)
    cost_materials: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cost_hardware: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cost_works: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    cost_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    price_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id")

    @property
    def markup_income(self) -> Decimal:
        return (self.price_total or 0) - (self.cost_total or 0)


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(120), default="")  # изделие: «Кухня», «Шкаф-купе»
    kind: Mapped[ItemKind] = mapped_column(Enum(ItemKind, native_enum=False, length=16))
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(16))
    height_mm: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    pieces: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    quantity: Mapped[Decimal] = mapped_column(Qty)  # итоговое количество в единицах unit
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    order: Mapped[Order] = relationship(back_populates="items")


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship()


class Estimate(Base):
    __tablename__ = "estimates"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"))
    total: Mapped[Decimal] = mapped_column(Money)
    file_path: Mapped[str] = mapped_column(String(300))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id"), index=True)  # NULL = стандартная
    name: Mapped[str] = mapped_column(String(80))
    sort: Mapped[int] = mapped_column(Integer, default=100)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    enterprise_id: Mapped[int] = mapped_column(ForeignKey("enterprises.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("expense_categories.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    spent_on: Mapped[date] = mapped_column(Date, index=True)
    comment: Mapped[str | None] = mapped_column(String(500))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped[ExpenseCategory] = relationship()


class MaterialUsage(Base):
    """Плановый/фактический расход и отходы по заказу (PRO, ENTERPRISE). Не складской учёт."""
    __tablename__ = "material_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(16))
    planned_qty: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    actual_qty: Mapped[Decimal | None] = mapped_column(Qty)
    waste_qty: Mapped[Decimal | None] = mapped_column(Qty)
    unit_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    comment: Mapped[str | None] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- infra

class StoredFile(Base):
    """Файлы (PDF-сметы, чеки, логотипы) хранятся в БД: так они не теряются на хостингах
    с временной файловой системой (Render Free и др.) и попадают в резервную копию базы.
    Поля logo_path / receipt_path / file_path других таблиц содержат ключ (id) записи."""
    __tablename__ = "stored_files"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))          # estimate | receipt | logo
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    enterprise_id: Mapped[int | None] = mapped_column(ForeignKey("enterprises.id"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(32))
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()


class Notification(Base):
    """Очередь уведомлений. Сервисы пишут сюда, воркер бота доставляет в Telegram."""
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_pending", "sent_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class LoginRequest(Base):
    """Вход на сайт подтверждением в Telegram-боте."""
    __tablename__ = "login_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # передаётся в бота
    poll_secret_hash: Mapped[str] = mapped_column(String(64))                 # известен только браузеру
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProcessedUpdate(Base):
    """Защита от повторной обработки одного Telegram-апдейта."""
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
