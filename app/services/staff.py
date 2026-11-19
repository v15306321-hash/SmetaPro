"""Сотрудники и приглашения (ТЗ, раздел 12)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Conflict, LimitReached, NotFound, PermissionDenied, ValidationError
from app.core.permissions import ALL_PERMISSIONS, OWNER_ONLY, ROLE_TITLES
from app.core.security import short_code
from app.models import Invitation, Membership, Role, User, utcnow
from app.services import tariffs
from app.services.accounts import Ctx, invitation_ttl
from app.services.audit import log, notify


def members(ctx: Ctx) -> list[Membership]:
    ctx.require("staff.view")
    return list(ctx.db.scalars(select(Membership).where(Membership.enterprise_id == ctx.eid,
                                                        Membership.is_active.is_(True))
                               .order_by(Membership.created_at)))


def active_count(db: Session, enterprise_id: int) -> int:
    return db.scalar(select(func.count(Membership.id)).where(Membership.enterprise_id == enterprise_id,
                                                             Membership.is_active.is_(True))) or 0


def _check_seat(db: Session, enterprise_id: int) -> None:
    sub = tariffs.require_active(db, enterprise_id)
    limit = sub.plan.users_limit
    if active_count(db, enterprise_id) >= limit:
        extra = " На тарифе START работает только владелец." if limit == 1 else ""
        raise LimitReached(f"Достигнут лимит пользователей тарифа {sub.plan.title}: {limit}, включая владельца.{extra} "
                           "Перейдите на тариф выше, чтобы добавить сотрудников.", limit=limit)


def invite(ctx: Ctx, role: Role) -> Invitation:
    ctx.require("staff.manage")
    if role == Role.owner:
        raise ValidationError("Владелец у предприятия один.")
    _check_seat(ctx.db, ctx.eid)
    inv = Invitation(code=short_code(10), enterprise_id=ctx.eid, role=role, created_by_id=ctx.user.id,
                     expires_at=utcnow() + invitation_ttl())
    ctx.db.add(inv)
    ctx.db.flush()
    log(ctx.db, "staff.invited", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="invitation",
        entity_id=inv.id, role=role.value)
    return inv


def invite_link(inv: Invitation) -> str:
    return f"https://t.me/{get_settings().bot_username}?start=inv_{inv.code}"


def accept(db: Session, user: User, code: str) -> Membership:
    inv = db.scalar(select(Invitation).where(Invitation.code == code.strip()))
    if not inv or inv.revoked or inv.used_at or inv.expires_at < utcnow():
        raise NotFound("Приглашение недействительно или истекло. Попросите владельца отправить новое.")
    existing = db.scalar(select(Membership).where(Membership.user_id == user.id,
                                                  Membership.enterprise_id == inv.enterprise_id))
    if existing and existing.is_active:
        raise Conflict("Вы уже состоите в этом предприятии.")
    _check_seat(db, inv.enterprise_id)
    if existing:
        existing.is_active = True
        existing.role = inv.role
        existing.permissions = None
        m = existing
    else:
        m = Membership(user_id=user.id, enterprise_id=inv.enterprise_id, role=inv.role)
        db.add(m)
    inv.used_by_id = user.id
    inv.used_at = utcnow()
    user.active_enterprise_id = inv.enterprise_id
    db.flush()
    log(db, "staff.joined", actor_id=user.id, enterprise_id=inv.enterprise_id, entity="membership",
        entity_id=m.id, role=inv.role.value)
    ent = m.enterprise
    notify(db, ent.owner_id, f"👥 {user.full_name or user.telegram_username or 'Сотрудник'} присоединился к "
                             f"«{ent.name}» как {ROLE_TITLES[inv.role].lower()}.")
    notify(db, user.id, f"Вы добавлены в предприятие «{ent.name}» ({ROLE_TITLES[inv.role]}).")
    return m


def _member(ctx: Ctx, member_id: int) -> Membership:
    m = ctx.db.get(Membership, member_id)
    if not m or m.enterprise_id != ctx.eid or not m.is_active:
        raise NotFound("Сотрудник не найден.")
    return m


def change_role(ctx: Ctx, member_id: int, role: Role, permissions: list[str] | None = None) -> Membership:
    ctx.require("staff.manage")
    m = _member(ctx, member_id)
    if m.role == Role.owner or role == Role.owner:
        raise PermissionDenied("Роль владельца изменить нельзя.")
    m.role = role
    if role == Role.admin and permissions is not None:
        m.permissions = [p for p in permissions if p in ALL_PERMISSIONS and p not in OWNER_ONLY]
    elif role != Role.admin:
        m.permissions = None
    log(ctx.db, "staff.role", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="membership", entity_id=m.id,
        role=role.value, permissions=m.permissions)
    notify(ctx.db, m.user_id, f"Ваш доступ в «{ctx.enterprise.name}» изменён: {ROLE_TITLES[role]}.")
    return m


def remove(ctx: Ctx, member_id: int) -> None:
    ctx.require("staff.manage")
    m = _member(ctx, member_id)
    if m.role == Role.owner:
        raise PermissionDenied("Владельца удалить нельзя.")
    m.is_active = False
    if m.user.active_enterprise_id == ctx.eid:
        m.user.active_enterprise_id = None
    log(ctx.db, "staff.removed", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="membership", entity_id=m.id)
    notify(ctx.db, m.user_id, f"Ваш доступ к предприятию «{ctx.enterprise.name}» отозван.")


def pending_invitations(ctx: Ctx) -> list[Invitation]:
    ctx.require("staff.view")
    return list(ctx.db.scalars(select(Invitation).where(Invitation.enterprise_id == ctx.eid,
                                                        Invitation.used_at.is_(None), Invitation.revoked.is_(False),
                                                        Invitation.expires_at > utcnow())))


def revoke_invitation(ctx: Ctx, inv_id: int) -> None:
    ctx.require("staff.manage")
    inv = ctx.db.get(Invitation, inv_id)
    if not inv or inv.enterprise_id != ctx.eid:
        raise NotFound("Приглашение не найдено.")
    inv.revoked = True
