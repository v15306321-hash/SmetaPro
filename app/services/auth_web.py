"""Вход на сайт: подтверждение в Telegram-боте (основной способ) или Telegram Login Widget.
Сессии хранятся в БД (в cookie — только случайный токен, в БД — его SHA-256), их можно отозвать."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import NotFound, ValidationError
from app.core.security import sha256, token_urlsafe, verify_telegram_login
from app.models import LoginRequest, User, WebSession, utcnow
from app.services import accounts
from app.services.audit import log

LOGIN_TTL = timedelta(minutes=10)


def create_login_request(db: Session) -> tuple[str, str]:
    token = token_urlsafe(18)
    poll_secret = token_urlsafe(24)
    db.add(LoginRequest(token=token, poll_secret_hash=sha256(poll_secret), expires_at=utcnow() + LOGIN_TTL))
    db.flush()
    return token, poll_secret


def confirm_login(db: Session, user: User, token: str) -> None:
    """Вызывается ботом, когда пользователь нажал «Подтвердить вход»."""
    lr = db.scalar(select(LoginRequest).where(LoginRequest.token == token))
    if not lr or lr.expires_at < utcnow() or lr.confirmed_at:
        raise NotFound("Ссылка для входа устарела. Обновите страницу входа на сайте.")
    lr.user_id = user.id
    lr.confirmed_at = utcnow()
    log(db, "auth.web_login_confirmed", actor_id=user.id)


def poll_login(db: Session, token: str, poll_secret: str) -> User | None:
    lr = db.scalar(select(LoginRequest).where(LoginRequest.token == token))
    if not lr or lr.poll_secret_hash != sha256(poll_secret) or lr.expires_at < utcnow():
        raise NotFound("Запрос входа не найден или истёк.")
    if not lr.confirmed_at:
        return None
    # одноразовое использование
    res = db.execute(update(LoginRequest).where(LoginRequest.id == lr.id, LoginRequest.consumed_at.is_(None))
                     .values(consumed_at=utcnow()))
    if res.rowcount != 1:
        raise NotFound("Запрос входа уже использован.")
    return db.get(User, lr.user_id)


def login_widget(db: Session, data: dict) -> User:
    if not verify_telegram_login(data, get_settings().bot_token):
        raise ValidationError("Не удалось подтвердить данные Telegram.")
    full_name = " ".join(x for x in [data.get("first_name"), data.get("last_name")] if x)
    user, _ = accounts.get_or_create_telegram_user(db, int(data["id"]), username=data.get("username"),
                                                   full_name=full_name)
    return user


def create_session(db: Session, user: User, user_agent: str | None, ip: str | None) -> str:
    raw = token_urlsafe(32)
    db.add(WebSession(token_hash=sha256(raw), user_id=user.id, user_agent=(user_agent or "")[:300], ip=ip,
                      expires_at=utcnow() + timedelta(days=get_settings().session_ttl_days)))
    log(db, "auth.session_created", actor_id=user.id, ip=ip)
    return raw


def user_by_session(db: Session, raw: str | None) -> tuple[User, WebSession] | None:
    if not raw:
        return None
    s = db.scalar(select(WebSession).where(WebSession.token_hash == sha256(raw)))
    now = utcnow()
    if not s or s.revoked_at or s.expires_at < now:
        return None
    if (now - s.last_seen_at).total_seconds() > 300:
        s.last_seen_at = now
    user = db.get(User, s.user_id)
    return (user, s) if user else None


def sessions(db: Session, user: User) -> list[WebSession]:
    return list(db.scalars(select(WebSession).where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None),
                                                    WebSession.expires_at > utcnow())
                           .order_by(WebSession.last_seen_at.desc())))


def revoke(db: Session, user: User, session_id: int | None = None, all_except: int | None = None) -> int:
    q = update(WebSession).where(WebSession.user_id == user.id, WebSession.revoked_at.is_(None))
    if session_id is not None:
        q = q.where(WebSession.id == session_id)
    if all_except is not None:
        q = q.where(WebSession.id != all_except)
    n = db.execute(q.values(revoked_at=utcnow())).rowcount
    log(db, "auth.sessions_revoked", actor_id=user.id, count=n)
    return n
