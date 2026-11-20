"""Сотрудники цеха: должность, оплата, доступ к расчётам в боте, увольнение и уход.

Сотрудник без доступа — запись для расчёта оплаты (его можно назначать на заказы).
Сотрудник с доступом получает ссылку-приглашение и работает в боте как «Сотрудник» владельца:
составляет расчёты и сметы, но не видит подписку, сотрудников и настройки. Подписка ему не нужна —
он работает по тарифу владельца; число таких людей ограничено лимитом пользователей тарифа.
"""
from __future__ import annotations

from sqlalchemy import select

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.fmt import D, money, qty
from app.models import Employee, Membership, Role, utcnow
from app.services import staff
from app.services.accounts import Ctx
from app.services.audit import log, notify

POSITIONS = ["Дизайнер", "Конструктор", "Менеджер", "Мастер", "Помощник мастера", "Бухгалтер"]
PAY_PERCENT = "percent"
PAY_FIXED = "fixed"


def pay_text(e: Employee) -> str:
    return f"{qty(e.pay_rate)}% от заказа" if e.pay_type == PAY_PERCENT else f"{money(e.pay_rate)} за заказ"


def list_active(ctx: Ctx) -> list[Employee]:
    if not (ctx.can("orders.edit") or ctx.can("staff.view")):
        raise PermissionDenied("Недостаточно прав.")
    return list(ctx.db.scalars(select(Employee).where(Employee.enterprise_id == ctx.eid,
                                                      Employee.is_active.is_(True)).order_by(Employee.id)))


def get(ctx: Ctx, employee_id: int) -> Employee:
    e = ctx.db.get(Employee, employee_id)
    if not e or e.enterprise_id != ctx.eid:
        raise NotFound("Сотрудник не найден.")
    return e


def create(ctx: Ctx, *, name: str, position: str, pay_type: str, pay_rate) -> Employee:
    ctx.require("staff.manage")
    name, position = (name or "").strip()[:120], (position or "").strip()[:80]
    if len(name) < 2:
        raise ValidationError("Укажите имя сотрудника.")
    if len(position) < 2:
        raise ValidationError("Укажите должность.")
    if pay_type not in (PAY_PERCENT, PAY_FIXED):
        raise ValidationError("Выберите способ оплаты.")
    rate = D(pay_rate)
    if rate < 0 or (pay_type == PAY_PERCENT and rate > 100):
        raise ValidationError("Процент — от 0 до 100." if pay_type == PAY_PERCENT else "Сумма не может быть меньше нуля.")
    e = Employee(enterprise_id=ctx.eid, name=name, position=position, pay_type=pay_type, pay_rate=rate)
    ctx.db.add(e)
    ctx.db.flush()
    log(ctx.db, "employee.created", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="employee", entity_id=e.id,
        position=position, pay_type=pay_type, rate=str(rate))
    return e


def grant_access(ctx: Ctx, employee_id: int) -> str:
    """Ссылка-приглашение в бота для сотрудника. Проверяется лимит пользователей тарифа."""
    e = get(ctx, employee_id)
    if e.user_id:
        raise ValidationError(f"У {e.name} уже есть доступ.")
    inv = staff.invite(ctx, Role.employee)
    inv.employee_id = e.id
    return staff.invite_link(inv)


def fire(ctx: Ctx, employee_id: int) -> Employee:
    ctx.require("staff.manage")
    e = get(ctx, employee_id)
    if not e.is_active:
        return e
    e.is_active = False
    e.fired_at = utcnow()
    if e.user_id:
        m = ctx.db.scalar(select(Membership).where(Membership.user_id == e.user_id,
                                                   Membership.enterprise_id == ctx.eid,
                                                   Membership.is_active.is_(True)))
        if m and m.role != Role.owner:
            m.is_active = False
            if m.user.active_enterprise_id == ctx.eid:
                m.user.active_enterprise_id = None
            notify(ctx.db, e.user_id, f"Вы больше не работаете в «{ctx.enterprise.name}». Доступ к расчётам закрыт.")
    log(ctx.db, "employee.fired", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="employee", entity_id=e.id)
    return e


def leave(ctx: Ctx) -> str:
    """Сотрудник сам уходит из цеха (кнопка «🚪 Уйти из цеха»)."""
    if not ctx.membership or ctx.role == Role.owner:
        raise PermissionDenied("Владелец не может уйти из своего предприятия.")
    ent_name = ctx.enterprise.name
    ctx.membership.is_active = False
    for e in ctx.db.scalars(select(Employee).where(Employee.enterprise_id == ctx.eid,
                                                   Employee.user_id == ctx.user.id, Employee.is_active.is_(True))):
        e.is_active = False
        e.fired_at = utcnow()
    ctx.user.active_enterprise_id = None
    log(ctx.db, "employee.left", actor_id=ctx.user.id, enterprise_id=ctx.eid)
    notify(ctx.db, ctx.enterprise.owner_id, f"🚪 {ctx.user.full_name or 'Сотрудник'} ушёл из «{ent_name}».")
    return ent_name
