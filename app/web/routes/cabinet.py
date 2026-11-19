"""Веб-кабинет предприятия (ТЗ 16.4) + аналитика и просмотр заказов/смет."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.errors import DomainError, FeatureUnavailable, NotFound
from app.core.fmt import TASHKENT_OFFSET, D
from app.core.permissions import ALL_PERMISSIONS, OWNER_ONLY
from app.db import get_db
from app.models import OrderStatus, Role, User
from app.services import (accounts, analytics, auth_web, estimates, files, orders, payments, referrals, staff,
                          tariffs)
from app.services.accounts import Ctx
from app.web.deps import flash_redirect, render, require_ctx, require_user, verify_csrf

router = APIRouter(prefix="/cabinet", include_in_schema=False)
POST = dict(dependencies=[Depends(verify_csrf)])


def today_local() -> date:
    return (datetime.utcnow() + TASHKENT_OFFSET).date()


def page(request: Request, ctx: Ctx, name: str, section: str, **kw):
    kw.setdefault("usage", tariffs.usage(ctx.db, ctx.eid) if ctx.enterprise else None)
    return render(request, name, user=ctx.user, ctx=ctx, section=section,
                  memberships=accounts.memberships(ctx.db, ctx.user), **kw)


def _need_enterprise(ctx: Ctx):
    if not ctx.enterprise:
        raise NotFound("Сначала зарегистрируйте предприятие.")


# ---------------------------------------------------------------- dashboard / onboarding

@router.get("")
def dashboard(request: Request, ctx: Ctx = Depends(require_ctx)):
    if not ctx.enterprise:
        return render(request, "cabinet/onboarding.html", user=ctx.user, section="dashboard")
    db = ctx.db
    data = dict(recent_orders=orders.list_orders(ctx, limit=6) if ctx.can("orders.view") else [],
                recent_estimates=estimates.recent(ctx, 5) if ctx.can("orders.view") else [])
    if ctx.can("analytics.view") and tariffs.has_feature(db, ctx.eid, "analytics"):
        start, end = analytics.resolve_period("month", today_local())
        ps, pe = analytics.previous_range(start, end)
        data.update(analytics_on=True, period=(start, end),
                    summary=analytics.summary(ctx, start, end), prev=analytics.summary(ctx, ps, pe),
                    spark=analytics.daily_series(ctx, start, end),
                    series=analytics.monthly_series(ctx, 7, today_local()),
                    materials=analytics.materials_breakdown(ctx, start, end),
                    margins=analytics.margin_by_section(ctx, start, end))
    else:
        data.update(analytics_on=False, analytics_plan=tariffs.min_plan_for(db, "analytics"))
    return page(request, ctx, "cabinet/dashboard.html", "dashboard", **data)


@router.post("/onboarding", **POST)
def onboarding(request: Request, name: str = Form(...), phone: str = Form(""), address: str = Form(""),
               user: User = Depends(require_user), db: Session = Depends(get_db)):
    ref = request.cookies.get("fe_ref")
    if ref and not user.referred_by_id:
        accounts.attach_referral(db, user, ref)
    accounts.register_enterprise(db, user, name=name, phone=phone, address=address)
    return flash_redirect("/cabinet/subscription", ok="Предприятие зарегистрировано. Выберите тариф.")


@router.post("/switch", **POST)
def switch(enterprise_id: int = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    accounts.switch_enterprise(db, user, enterprise_id)
    return flash_redirect("/cabinet")


# ---------------------------------------------------------------- analytics

@router.get("/analytics")
def analytics_page(request: Request, period: str = "month", start: date | None = None, end: date | None = None,
                   completed: int = 0, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    try:
        s, e = analytics.resolve_period(period, today_local(), start, end)
        summary = analytics.summary(ctx, s, e, completed_only=bool(completed))
    except FeatureUnavailable as exc:
        return page(request, ctx, "cabinet/locked.html", "analytics", feature="analytics", message=exc.message,
                    min_plan=exc.extra.get("min_plan"))
    ps, pe = analytics.previous_range(s, e)
    return page(request, ctx, "cabinet/analytics.html", "analytics", period=period, start=s, end=e,
                completed=completed, summary=summary,
                prev=analytics.summary(ctx, ps, pe, completed_only=bool(completed)),
                spark=analytics.daily_series(ctx, s, e), series=analytics.monthly_series(ctx, 7, today_local()),
                materials=analytics.materials_breakdown(ctx, s, e),
                expenses=analytics.expenses_breakdown(ctx, s, e),
                margins=analytics.margin_by_section(ctx, s, e), top=analytics.top_orders(ctx, s, e),
                statuses_on=tariffs.has_feature(ctx.db, ctx.eid, "order_statuses"), periods=analytics.PERIODS)


# ---------------------------------------------------------------- orders (просмотр)

@router.get("/orders")
def orders_page(request: Request, status: str | None = None, q: str | None = None, p: int = 1,
                ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    st = OrderStatus(status) if status in OrderStatus.__members__ else None
    p = max(1, p)
    items = orders.list_orders(ctx, status=st, search=q, limit=31, offset=(p - 1) * 30)
    return page(request, ctx, "cabinet/orders.html", "orders", orders=items[:30], has_next=len(items) > 30, p=p,
                status=status, q=q or "", STATUS_TITLES=orders.STATUS_TITLES,
                statuses_on=tariffs.has_feature(ctx.db, ctx.eid, "order_statuses"))


@router.get("/orders/{order_id}")
def order_page(order_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    o = orders.get(ctx, order_id)
    return page(request, ctx, "cabinet/order.html", "orders", order=o, groups=orders.grouped_items(o),
                estimates=estimates.list_for_order(ctx, o.id), history=orders.status_history(ctx, o.id),
                STATUS_TITLES=orders.STATUS_TITLES)


@router.get("/estimates/{uid}.pdf")
def estimate_pdf(uid: str, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    est, path = estimates.get_file(ctx, uid)
    if not path.exists():
        raise NotFound("Файл сметы не найден.")
    return FileResponse(path, media_type="application/pdf", filename=f"{est.uid}.pdf")


# ---------------------------------------------------------------- subscription / payments

@router.get("/subscription")
def subscription_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    db = ctx.db
    plans = tariffs.list_plans(db)
    discount, _ = referrals.available_discount(db, ctx.user.id)
    return page(request, ctx, "cabinet/subscription.html", "subscription", plans=plans,
                methods=payments.list_methods(db), discount=discount,
                current=tariffs.current_subscription(db, ctx.eid), queued=tariffs.queued_subscriptions(db, ctx.eid),
                last=tariffs.last_subscription(db, ctx.eid), history=tariffs.subscription_history(db, ctx.eid),
                open_payment=next((x for x in payments.history(db, ctx.eid) if x.status in payments.OPEN_STATUSES),
                                  None))


@router.post("/subscription/request", **POST)
def subscription_request(plan: str = Form(...), method: str = Form(...), ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    pr = payments.create_request(ctx, plan, method)
    return flash_redirect(f"/cabinet/payments/{pr.id}")


@router.get("/payments")
def payments_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    return page(request, ctx, "cabinet/payments.html", "payments", payments=payments.history(ctx.db, ctx.eid),
                STATUS_TITLES=payments.STATUS_TITLES)


@router.get("/payments/{payment_id}")
def payment_page(payment_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    pr = payments.get_for_ctx(ctx, payment_id)
    return page(request, ctx, "cabinet/payment.html", "payments", pr=pr, STATUS_TITLES=payments.STATUS_TITLES)


@router.post("/payments/{payment_id}/receipt", **POST)
async def payment_receipt(payment_id: int, receipt: UploadFile = File(...), ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    data = await receipt.read(files.MAX_SIZE + 1)
    path = files.save_receipt(data, folder=f"receipts/{ctx.eid}")
    payments.attach_receipt(ctx, payment_id, path=path)
    return flash_redirect(f"/cabinet/payments/{payment_id}", ok="Чек отправлен на проверку.")


@router.post("/payments/{payment_id}/cancel", **POST)
def payment_cancel(payment_id: int, ctx: Ctx = Depends(require_ctx)):
    payments.cancel(ctx, payment_id)
    return flash_redirect("/cabinet/subscription", ok="Заявка отменена.")


# ---------------------------------------------------------------- staff

@router.get("/staff")
def staff_page(request: Request, invite: str | None = None, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    return page(request, ctx, "cabinet/staff.html", "staff", members=staff.members(ctx),
                invitations=staff.pending_invitations(ctx), invite_link=invite, Role=Role,
                ALL_PERMISSIONS={k: v for k, v in ALL_PERMISSIONS.items() if k not in OWNER_ONLY},
                link_for=staff.invite_link)


@router.post("/staff/invite", **POST)
def staff_invite(role: str = Form(...), ctx: Ctx = Depends(require_ctx)):
    inv = staff.invite(ctx, Role(role))
    return flash_redirect(f"/cabinet/staff?invite={staff.invite_link(inv)}", ok="Приглашение создано.")


@router.post("/staff/{member_id}/role", **POST)
async def staff_role(member_id: int, request: Request, role: str = Form(...), ctx: Ctx = Depends(require_ctx)):
    form = await request.form()
    perms = form.getlist("perm") if role == Role.admin.value else None
    staff.change_role(ctx, member_id, Role(role), perms)
    return flash_redirect("/cabinet/staff", ok="Права сотрудника обновлены.")


@router.post("/staff/{member_id}/remove", **POST)
def staff_remove(member_id: int, ctx: Ctx = Depends(require_ctx)):
    staff.remove(ctx, member_id)
    return flash_redirect("/cabinet/staff", ok="Доступ сотрудника отозван.")


@router.post("/staff/invitations/{inv_id}/revoke", **POST)
def staff_revoke(inv_id: int, ctx: Ctx = Depends(require_ctx)):
    staff.revoke_invitation(ctx, inv_id)
    return flash_redirect("/cabinet/staff", ok="Приглашение отозвано.")


# ---------------------------------------------------------------- referrals

@router.get("/referrals")
def referrals_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    return page(request, ctx, "cabinet/referrals.html", "referrals", ref=referrals.summary(ctx.db, ctx.user))


# ---------------------------------------------------------------- profile / settings

@router.get("/profile")
def profile_page(request: Request, ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    current = getattr(request.state, "web_session", None)
    return page(request, ctx, "cabinet/profile.html", "profile", sessions=auth_web.sessions(ctx.db, ctx.user),
                current_session=current.id if current else None,
                logo_allowed=tariffs.has_feature(ctx.db, ctx.eid, "pdf_logo"))


@router.post("/profile/enterprise", **POST)
def profile_enterprise(name: str = Form(...), phone: str = Form(""), address: str = Form(""),
                       email: str = Form(""), tax_id: str = Form(""), fixed_addition_label: str = Form(""),
                       ctx: Ctx = Depends(require_ctx)):
    accounts.update_enterprise(ctx, name=name, phone=phone, address=address, email=email, tax_id=tax_id,
                               fixed_addition_label=fixed_addition_label)
    return flash_redirect("/cabinet/profile", ok="Данные предприятия сохранены.")


@router.post("/profile/markups", **POST)
def profile_markups(materials: str = Form(...), hardware: str = Form(...), works: str = Form(...),
                    fixed: str = Form("0"), ctx: Ctx = Depends(require_ctx)):
    try:
        accounts.set_markups(ctx, materials=D(materials), hardware=D(hardware), works=D(works), fixed=D(fixed or 0))
    except DomainError:
        raise
    except Exception:
        raise DomainError("Проверьте введённые значения: нужны числа.")
    return flash_redirect("/cabinet/profile", ok="Наценки сохранены.")


@router.post("/profile/logo", **POST)
async def profile_logo(logo: UploadFile = File(...), ctx: Ctx = Depends(require_ctx)):
    data = await logo.read(files.MAX_SIZE + 1)
    accounts.set_logo(ctx, data, "")
    return flash_redirect("/cabinet/profile", ok="Логотип загружен.")


@router.get("/logo")
def logo(ctx: Ctx = Depends(require_ctx)):
    _need_enterprise(ctx)
    if not ctx.enterprise.logo_path:
        raise NotFound("Логотип не загружен.")
    return FileResponse(files.abs_path(ctx.enterprise.logo_path))


@router.post("/sessions/revoke", **POST)
def sessions_revoke(request: Request, session_id: int | None = Form(None), others: int = Form(0),
                    user: User = Depends(require_user), db: Session = Depends(get_db)):
    current = getattr(request.state, "web_session", None)
    if others and current:
        auth_web.revoke(db, user, all_except=current.id)
    elif session_id:
        auth_web.revoke(db, user, session_id=session_id)
    return flash_redirect("/cabinet/profile", ok="Сессии отозваны.")


_ = timedelta
