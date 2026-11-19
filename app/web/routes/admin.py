"""Административная панель платформы (ТЗ 17). Доступ — только суперадминистраторы."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import NotFound
from app.db import get_db
from app.models import Enterprise, Membership, PaymentRequest, PaymentStatus, User
from app.services import accounts, admin as admin_svc, files, payments, referrals, tariffs
from app.services.tariffs import FEATURE_TITLES
from app.web.deps import flash_redirect, render, require_user, verify_csrf

router = APIRouter(prefix="/admin", include_in_schema=False)
POST = dict(dependencies=[Depends(verify_csrf)])


def superadmin(user: User = Depends(require_user)) -> User:
    return admin_svc.require_superadmin(user)


def page(request: Request, admin: User, name: str, section: str, **kw):
    return render(request, name, user=admin, section=section, admin_mode=True, **kw)


@router.get("")
def dashboard(request: Request, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    return page(request, admin, "admin/dashboard.html", "dashboard", stats=admin_svc.stats(db),
                pending=admin_svc.payments(db, PaymentStatus.pending, 10), plans=tariffs.list_plans(db, False))


# ---------------------------------------------------------------- payments

@router.get("/payments")
def payments_page(request: Request, status: str | None = "pending", admin: User = Depends(superadmin),
                  db: Session = Depends(get_db)):
    st = PaymentStatus(status) if status in PaymentStatus.__members__ else None
    return page(request, admin, "admin/payments.html", "payments", items=admin_svc.payments(db, st, 200),
                status=status, STATUS_TITLES=payments.STATUS_TITLES, PaymentStatus=PaymentStatus)


@router.post("/payments/{pid}/approve", **POST)
def approve(pid: int, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    pr = payments.approve(db, admin, pid)
    return flash_redirect("/admin/payments", ok=f"Заявка {pr.uid} подтверждена, подписка активирована.")


@router.post("/payments/{pid}/reject", **POST)
def reject(pid: int, reason: str = Form(""), admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    pr = payments.reject(db, admin, pid, reason)
    return flash_redirect("/admin/payments", ok=f"Заявка {pr.uid} отклонена.")


@router.post("/payments/{pid}/refund", **POST)
def refund(pid: int, reason: str = Form(...), admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    pr = payments.refund(db, admin, pid, reason)
    return flash_redirect("/admin/payments?status=approved", ok=f"Возврат по {pr.uid} отмечен.")


@router.get("/payments/{pid}/receipt")
def receipt(pid: int, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    pr = db.get(PaymentRequest, pid)
    if not pr:
        raise NotFound("Заявка не найдена.")
    if pr.receipt_path:
        f = files.load(db, pr.receipt_path)
        return Response(f.data, media_type=f.content_type)
    if pr.receipt_file_id:
        token = get_settings().bot_token
        with httpx.Client(timeout=20) as c:
            info = c.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": pr.receipt_file_id}).json()
            if not info.get("ok"):
                raise NotFound("Файл недоступен в Telegram.")
            r = c.get(f"https://api.telegram.org/file/bot{token}/{info['result']['file_path']}")
        ctype = "application/pdf" if r.content.startswith(b"%PDF") else "image/jpeg"
        return Response(r.content, media_type=ctype)
    raise NotFound("Чек не приложен.")


# ---------------------------------------------------------------- users / enterprises

@router.get("/users")
def users_page(request: Request, q: str | None = None, admin: User = Depends(superadmin),
               db: Session = Depends(get_db)):
    return page(request, admin, "admin/users.html", "users", items=admin_svc.search_users(db, q), q=q or "")


@router.post("/users/{uid}/block", **POST)
def user_block(uid: int, blocked: int = Form(...), reason: str = Form(""), admin: User = Depends(superadmin),
               db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise NotFound("Пользователь не найден.")
    accounts.block_user(db, admin, u, bool(blocked), reason or None)
    return flash_redirect("/admin/users", ok="Статус пользователя обновлён.")


@router.get("/enterprises")
def enterprises_page(request: Request, q: str | None = None, admin: User = Depends(superadmin),
                     db: Session = Depends(get_db)):
    items = admin_svc.search_enterprises(db, q)
    subs = {e.id: tariffs.current_subscription(db, e.id) for e in items}
    counts = {e.id: db.query(Membership).filter_by(enterprise_id=e.id, is_active=True).count() for e in items}
    return page(request, admin, "admin/enterprises.html", "enterprises", items=items, subs=subs, counts=counts,
                q=q or "", plans=tariffs.list_plans(db))


@router.post("/enterprises/{eid}/block", **POST)
def enterprise_block(eid: int, blocked: int = Form(...), reason: str = Form(""), admin: User = Depends(superadmin),
                     db: Session = Depends(get_db)):
    e = db.get(Enterprise, eid)
    if not e:
        raise NotFound("Предприятие не найдено.")
    accounts.block_enterprise(db, admin, e, bool(blocked), reason or None)
    return flash_redirect("/admin/enterprises", ok="Статус предприятия обновлён.")


@router.post("/enterprises/{eid}/grant", **POST)
def enterprise_grant(eid: int, plan: str = Form(...), reason: str = Form(...), admin: User = Depends(superadmin),
                     db: Session = Depends(get_db)):
    admin_svc.grant_subscription(db, admin, eid, plan, reason)
    return flash_redirect("/admin/enterprises", ok="Подписка активирована вручную.")


# ---------------------------------------------------------------- plans / methods

@router.get("/plans")
def plans_page(request: Request, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    return page(request, admin, "admin/plans.html", "plans", plans=tariffs.list_plans(db, False),
                methods=payments.list_methods(db, False), FEATURES=FEATURE_TITLES)


@router.post("/plans/{code}", **POST)
async def plan_update(code: str, request: Request, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    f = await request.form()
    admin_svc.update_plan(db, admin, code, price=f.get("price"), estimates_limit=f.get("estimates_limit"),
                          materials_limit=f.get("materials_limit"), users_limit=f.get("users_limit"),
                          subtitle=f.get("subtitle"), is_active=bool(f.get("is_active")),
                          features={k: bool(f.get(f"f_{k}")) for k in FEATURE_TITLES})
    return flash_redirect("/admin/plans", ok=f"Тариф {code} обновлён.")


@router.post("/methods/{code}", **POST)
async def method_update(code: str, request: Request, admin: User = Depends(superadmin),
                        db: Session = Depends(get_db)):
    f = await request.form()
    payments.update_method(db, admin, code, title=f.get("title"), details=f.get("details"), holder=f.get("holder"),
                           is_active=bool(f.get("is_active")))
    return flash_redirect("/admin/plans", ok="Реквизиты обновлены.")


# ---------------------------------------------------------------- referrals / audit

@router.get("/referrals")
def referrals_page(request: Request, admin: User = Depends(superadmin), db: Session = Depends(get_db)):
    return page(request, admin, "admin/referrals.html", "referrals", items=admin_svc.referrals_list(db))


@router.post("/referrals/{rid}/annul", **POST)
def referral_annul(rid: int, reason: str = Form(...), admin: User = Depends(superadmin),
                   db: Session = Depends(get_db)):
    referrals.annul(db, admin, rid, reason)
    return flash_redirect("/admin/referrals", ok="Начисление аннулировано.")


@router.get("/audit")
def audit_page(request: Request, action: str | None = None, admin: User = Depends(superadmin),
               db: Session = Depends(get_db)):
    return page(request, admin, "admin/audit.html", "audit", items=admin_svc.audit_entries(db, action),
                action=action or "")


_ = select
