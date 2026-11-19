"""Пользователи, предприятия и контекст доступа (tenant isolation)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Blocked, Conflict, NotFound, PermissionDenied, ValidationError
from app.core.permissions import permissions_for
from app.core.security import short_code
from app.models import Enterprise, Membership, Referral, ReferralStatus, Role, User
from app.services.audit import log, notify


@dataclass
class Ctx:
    """Контекст запроса: кто действует и в каком предприятии. Передаётся во все сервисы."""
    db: Session
    user: User
    enterprise: Enterprise | None = None
    membership: Membership | None = None
    perms: set[str] = field(default_factory=set)

    @property
    def eid(self) -> int:
        if not self.enterprise:
            raise NotFound("Сначала зарегистрируйте предприятие.")
        return self.enterprise.id

    @property
    def role(self) -> Role | None:
        return self.membership.role if self.membership else None

    @property
    def is_owner(self) -> bool:
        return self.role == Role.owner

    def can(self, perm: str) -> bool:
        return perm in self.perms

    def require(self, perm: str) -> None:
        if self.enterprise is None:
            raise NotFound("Сначала зарегистрируйте предприятие.")
        if perm not in self.perms:
            raise PermissionDenied("Недостаточно прав для этого действия. Обратитесь к владельцу предприятия.")


def _unique_ref_code(db: Session) -> str:
    while True:
        code = short_code(8)
        if not db.scalar(select(User.id).where(User.referral_code == code)):
            return code


def get_or_create_telegram_user(db: Session, telegram_id: int, *, username: str | None = None,
                                full_name: str = "", ref_code: str | None = None) -> tuple[User, bool]:
    user = db.scalar(select(User).where(User.telegram_id == telegram_id))
    created = False
    if user is None:
        user = User(telegram_id=telegram_id, telegram_username=username, full_name=full_name or "",
                    referral_code=_unique_ref_code(db),
                    is_superadmin=telegram_id in get_settings().superadmin_ids)
        db.add(user)
        db.flush()
        created = True
        log(db, "user.registered", actor_id=user.id, entity="user", entity_id=user.id)
        if ref_code:
            attach_referral(db, user, ref_code)
    else:
        changed = False
        if username != user.telegram_username:
            user.telegram_username = username
            changed = True
        if full_name and full_name != user.full_name:
            user.full_name = full_name
            changed = True
        if telegram_id in get_settings().superadmin_ids and not user.is_superadmin:
            user.is_superadmin = True
            changed = True
        if changed:
            db.flush()
    return user, created


def attach_referral(db: Session, user: User, ref_code: str) -> Referral | None:
    """Связать нового пользователя с пригласившим. Самоприглашение и повтор — игнорируются."""
    referrer = db.scalar(select(User).where(User.referral_code == ref_code.strip().upper()))
    if not referrer or referrer.id == user.id or user.referred_by_id:
        return None
    if db.scalar(select(Referral.id).where(Referral.referred_user_id == user.id)):
        return None
    # Пользователь, уже состоящий в предприятии пригласившего, не может быть его рефералом
    if db.scalar(select(Membership.id).join(Enterprise, Enterprise.id == Membership.enterprise_id)
                 .where(Membership.user_id == user.id, Enterprise.owner_id == referrer.id)):
        return None
    user.referred_by_id = referrer.id
    ref = Referral(referrer_id=referrer.id, referred_user_id=user.id)
    db.add(ref)
    db.flush()
    log(db, "referral.registered", actor_id=user.id, entity="referral", entity_id=ref.id, referrer_id=referrer.id)
    return ref


def _norm_phone(phone: str | None) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())[-9:]


def register_enterprise(db: Session, user: User, *, name: str, phone: str | None = None,
                        address: str | None = None, email: str | None = None) -> Enterprise:
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 200:
        raise ValidationError("Название предприятия должно содержать от 2 до 200 символов.")
    if user.is_blocked:
        raise Blocked("Аккаунт заблокирован.")
    # Один аккаунт — одно собственное предприятие (защита от обхода лимитов/реферальных правил)
    if db.scalar(select(Enterprise.id).where(Enterprise.owner_id == user.id)):
        raise Conflict("У вас уже есть зарегистрированное предприятие.")
    ent = Enterprise(name=name, phone=(phone or "").strip() or None, address=(address or "").strip() or None,
                     email=(email or "").strip() or None, owner_id=user.id)
    db.add(ent)
    db.flush()
    db.add(Membership(user_id=user.id, enterprise_id=ent.id, role=Role.owner))
    user.active_enterprise_id = ent.id
    if phone and not user.phone:
        user.phone = phone

    ref = db.scalar(select(Referral).where(Referral.referred_user_id == user.id,
                                           Referral.status == ReferralStatus.registered))
    if ref and ref.referred_enterprise_id is None:
        ref.referred_enterprise_id = ent.id
        # Признаки дубликата: совпадение телефона с предприятием пригласившего
        ref_ent = db.scalar(select(Enterprise).where(Enterprise.owner_id == ref.referrer_id))
        if ref_ent and _norm_phone(ref_ent.phone) and _norm_phone(ref_ent.phone) == _norm_phone(phone):
            ref.suspicious = True
            ref.note = "Телефон совпадает с предприятием пригласившего"
    db.flush()
    log(db, "enterprise.registered", actor_id=user.id, enterprise_id=ent.id, entity="enterprise", entity_id=ent.id)
    notify(db, user.id, f"✅ Предприятие «{ent.name}» зарегистрировано. Выберите тариф в разделе «Подписка», "
                        "чтобы начать расчёты.")
    return ent


def memberships(db: Session, user: User) -> list[Membership]:
    return list(db.scalars(select(Membership).where(Membership.user_id == user.id,
                                                    Membership.is_active.is_(True))))


def load_ctx(db: Session, user: User, check_blocked: bool = True) -> Ctx:
    if check_blocked and user.is_blocked:
        raise Blocked(f"Аккаунт заблокирован. {user.block_reason or ''}".strip())
    ms = memberships(db, user)
    m = next((x for x in ms if x.enterprise_id == user.active_enterprise_id), None) or (ms[0] if ms else None)
    if m is None:
        return Ctx(db=db, user=user)
    if m.enterprise_id != user.active_enterprise_id:
        user.active_enterprise_id = m.enterprise_id
    ent = m.enterprise
    if check_blocked and ent.is_blocked:
        raise Blocked(f"Предприятие заблокировано. {ent.block_reason or ''}".strip())
    return Ctx(db=db, user=user, enterprise=ent, membership=m, perms=permissions_for(m.role, m.permissions))


def switch_enterprise(db: Session, user: User, enterprise_id: int) -> None:
    if not db.scalar(select(Membership.id).where(Membership.user_id == user.id, Membership.is_active.is_(True),
                                                 Membership.enterprise_id == enterprise_id)):
        raise PermissionDenied("Нет доступа к этому предприятию.")
    user.active_enterprise_id = enterprise_id


def update_enterprise(ctx: Ctx, **fields) -> Enterprise:
    ctx.require("settings.edit")
    ent = ctx.enterprise
    allowed = {"name", "phone", "address", "email", "tax_id", "fixed_addition_label"}
    for k, v in fields.items():
        if k in allowed and v is not None:
            v = str(v).strip()
            if k == "name" and len(v) < 2:
                raise ValidationError("Название слишком короткое.")
            if k == "name":
                setattr(ent, k, v[:200])
            else:
                default = "Дополнительные услуги" if k == "fixed_addition_label" else None
                setattr(ent, k, v[:300] or default)
    log(ctx.db, "enterprise.updated", actor_id=ctx.user.id, enterprise_id=ent.id, fields=list(fields))
    return ent


def set_markups(ctx: Ctx, *, materials=None, hardware=None, works=None, fixed=None) -> Enterprise:
    """Индивидуальные наценки — доступны во всех тарифах."""
    from app.core.fmt import D
    from app.services import tariffs
    ctx.require("settings.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "custom_markups")
    ent = ctx.enterprise
    for attr, val in (("markup_materials", materials), ("markup_hardware", hardware), ("markup_works", works)):
        if val is not None:
            v = D(val)
            if v < 0 or v > 1000:
                raise ValidationError("Наценка должна быть от 0 до 1000%.")
            setattr(ent, attr, v)
    if fixed is not None:
        v = D(fixed)
        if v < 0:
            raise ValidationError("Фиксированная сумма не может быть отрицательной.")
        ent.fixed_addition = v
    log(ctx.db, "enterprise.markups", actor_id=ctx.user.id, enterprise_id=ent.id,
        materials=str(ent.markup_materials), hardware=str(ent.markup_hardware),
        works=str(ent.markup_works), fixed=str(ent.fixed_addition))
    return ent


def set_logo(ctx: Ctx, data: bytes, ext: str) -> str:
    from app.services import tariffs
    from app.services.files import save_image
    ctx.require("settings.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "pdf_logo")
    path = save_image(data, ext, folder=f"logos/{ctx.eid}")
    ctx.enterprise.logo_path = path
    log(ctx.db, "enterprise.logo", actor_id=ctx.user.id, enterprise_id=ctx.eid)
    return path


def block_user(db: Session, admin: User, user: User, blocked: bool, reason: str | None) -> None:
    user.is_blocked = blocked
    user.block_reason = reason if blocked else None
    log(db, "admin.user.block" if blocked else "admin.user.unblock", actor_id=admin.id,
        entity="user", entity_id=user.id, reason=reason)


def block_enterprise(db: Session, admin: User, ent: Enterprise, blocked: bool, reason: str | None) -> None:
    ent.is_blocked = blocked
    ent.block_reason = reason if blocked else None
    log(db, "admin.enterprise.block" if blocked else "admin.enterprise.unblock", actor_id=admin.id,
        enterprise_id=ent.id, entity="enterprise", entity_id=ent.id, reason=reason)
    if blocked:
        notify(db, ent.owner_id, f"⛔ Предприятие «{ent.name}» заблокировано администратором. Причина: {reason or '—'}")


def invitation_ttl() -> timedelta:
    return timedelta(days=3)
