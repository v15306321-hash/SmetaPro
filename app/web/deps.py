"""Зависимости веб-слоя: текущий пользователь, контекст предприятия, CSRF, шаблоны."""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import i18n
from app.config import get_settings
from app.core import fmt
from app.core.errors import DomainError, PermissionDenied
from app.core.permissions import ROLE_TITLES
from app.db import get_db
from app.models import User
from app.services import accounts, auth_web, tariffs
from app.services.accounts import Ctx

SESSION_COOKIE = "fe_session"

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(money=fmt.money, money0=fmt.money0, qty=fmt.qty, dt=fmt.dt_str, date=fmt.date_str,
                             words=fmt.amount_in_words)
templates.env.globals.update(settings=get_settings(), FEATURE_TITLES=tariffs.FEATURE_TITLES,
                             ROLE_TITLES=ROLE_TITLES)


class LoginRequired(Exception):
    pass


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    found = auth_web.user_by_session(db, session_token(request))
    if not found:
        return None
    user, sess = found
    request.state.web_session = sess
    return user


def require_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise LoginRequired()
    if user.is_blocked:
        raise PermissionDenied(f"Аккаунт заблокирован. {user.block_reason or ''}".strip())
    return user


def require_ctx(user: User = Depends(require_user), db: Session = Depends(get_db)) -> Ctx:
    return accounts.load_ctx(db, user)


def csrf_token(request: Request) -> str:
    raw = session_token(request) or "anon"
    return hmac.new(get_settings().secret_key.encode(), raw.encode(), "sha256").hexdigest()[:32]


async def verify_csrf(request: Request) -> None:
    """CSRF для форм кабинета/админки (cookie SameSite=Lax + токен формы)."""
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        form = await request.form()
        sent = form.get("csrf") or request.headers.get("x-csrf-token")
        if not sent or not hmac.compare_digest(str(sent), csrf_token(request)):
            raise DomainError("Сессия формы устарела. Обновите страницу и повторите действие.")


def render(request: Request, name: str, status_code: int = 200, **ctx) -> object:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx["csrf"] = csrf_token(request)
    ctx["request"] = request
    ctx["flash"] = request.query_params.get("ok")
    ctx["flash_error"] = request.query_params.get("err")
    from app.services import promo
    ctx.setdefault("promo_pct", promo.active_percent())
    ctx.setdefault("promo_banner", promo.banner())
    lang = resolve_lang(request, ctx.get("user"))
    ctx["lang"] = lang
    ctx["langs"] = i18n.LANGS
    html = templates.get_template(name).render(ctx)
    if lang != i18n.DEFAULT and not ctx.get("admin_mode"):   # админ-панель — только по-русски
        html = i18n.tr(html, lang)
    response = HTMLResponse(html, status_code=status_code)
    if request.query_params.get("lang"):
        response.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax")
    return response


def resolve_lang(request: Request, user=None) -> str:
    """?lang=… → cookie → язык пользователя (как в боте) → Accept-Language → русский."""
    q = request.query_params.get("lang")
    if q:
        return i18n.normalize(q)
    if request.cookies.get("lang"):
        return i18n.normalize(request.cookies["lang"])
    if user is not None and getattr(user, "language", None):
        return i18n.normalize(user.language)
    accept = (request.headers.get("accept-language") or "").lower()
    for code in ("uz", "kk", "kz"):
        if accept.startswith(code) or f",{code}" in accept.replace(" ", ""):
            return i18n.normalize(code)
    return i18n.DEFAULT


def flash_redirect(url: str, ok: str | None = None, err: str | None = None):
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse
    params = {}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (sep + urlencode(params) if params else ""), status_code=303)
