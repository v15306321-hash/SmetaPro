"""Вход на сайт через Telegram (ТЗ 16.3)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import NotFound, PermissionDenied
from app.db import get_db
from app.models import User
from app.services import accounts, auth_web
from app.web.deps import SESSION_COOKIE, optional_user, render, require_user, verify_csrf

router = APIRouter(include_in_schema=False)


def _safe_next(nxt: str | None) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/cabinet"


def _set_session(resp, raw: str):
    s = get_settings()
    resp.set_cookie(SESSION_COOKIE, raw, max_age=s.session_ttl_days * 86400, httponly=True,
                    secure=s.is_production, samesite="lax")
    return resp


def _client(request: Request):
    return request.headers.get("user-agent"), request.headers.get("x-real-ip") or (
        request.client.host if request.client else None)


@router.get("/login")
def login_page(request: Request, next: str | None = None, user: User | None = Depends(optional_user)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "public/login.html", user=None, next=_safe_next(next), page="login")


@router.post("/auth/start")
def auth_start(db: Session = Depends(get_db)):
    token, poll = auth_web.create_login_request(db)
    s = get_settings()
    return {"token": token, "poll": poll, "link": f"https://t.me/{s.bot_username}?start=login_{token}"}


@router.post("/auth/poll")
def auth_poll(request: Request, token: str = Form(...), poll: str = Form(...), db: Session = Depends(get_db)):
    try:
        user = auth_web.poll_login(db, token, poll)
    except NotFound as e:
        return JSONResponse({"status": "expired", "message": e.message}, 410)
    if not user:
        return {"status": "waiting"}
    if user.is_blocked:
        raise PermissionDenied("Аккаунт заблокирован.")
    raw = auth_web.create_session(db, user, *_client(request))
    return _set_session(JSONResponse({"status": "ok"}), raw)


@router.get("/auth/telegram")
def auth_widget(request: Request, next: str | None = None, db: Session = Depends(get_db)):
    data = {k: v for k, v in request.query_params.items() if k != "next"}
    user = auth_web.login_widget(db, data)
    ref = request.cookies.get("fe_ref")
    if ref and not user.referred_by_id and not user.active_enterprise_id:
        accounts.attach_referral(db, user, ref)
    raw = auth_web.create_session(db, user, *_client(request))
    return _set_session(RedirectResponse(_safe_next(next), status_code=303), raw)


@router.get("/auth/dev-login")
def dev_login(request: Request, tg: int = 1, name: str = "Dev User", admin: bool = False,
              db: Session = Depends(get_db)):
    s = get_settings()
    if not s.dev_login or s.is_production:
        raise NotFound("Страница не найдена.")
    user, _ = accounts.get_or_create_telegram_user(db, tg, full_name=name)
    if admin:
        user.is_superadmin = True
    raw = auth_web.create_session(db, user, *_client(request))
    return _set_session(RedirectResponse("/cabinet", status_code=303), raw)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    sess = getattr(request.state, "web_session", None)
    if sess:
        auth_web.revoke(db, user, session_id=sess.id)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
