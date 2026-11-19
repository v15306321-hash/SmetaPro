"""REST API v1 (JSON) для веб-кабинета и будущих веб-модулей. Документация: /api/docs.
Авторизация — cookie-сессия веб-кабинета. Все ответы ограничены предприятием пользователя."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.fmt import TASHKENT_OFFSET
from app.models import OrderStatus
from app.services import analytics, materials, orders, payments, referrals, tariffs
from app.services.accounts import Ctx
from app.web.deps import require_ctx

router = APIRouter(prefix="/api/v1", tags=["v1"])


class PlanOut(BaseModel):
    code: str
    title: str
    subtitle: str
    price: Decimal
    estimates_limit: int
    materials_limit: int
    users_limit: int
    features: dict


class OrderOut(BaseModel):
    id: int
    number: int
    title: str
    client_name: str
    status: str
    cost_total: Decimal
    price_total: Decimal
    created_at: datetime


@router.get("/plans", response_model=list[PlanOut], summary="Тарифы")
def plans(ctx: Ctx = Depends(require_ctx)):
    return [PlanOut.model_validate(p, from_attributes=True) for p in tariffs.list_plans(ctx.db)]


@router.get("/me", summary="Профиль, предприятие, роль, подписка и лимиты")
def me(ctx: Ctx = Depends(require_ctx)):
    u = ctx.user
    out = {"user": {"id": u.id, "full_name": u.full_name, "telegram_username": u.telegram_username},
           "enterprise": None}
    if ctx.enterprise:
        e = ctx.enterprise
        us = tariffs.usage(ctx.db, e.id)
        sub = us.get("subscription")
        out["enterprise"] = {"id": e.id, "name": e.name, "role": ctx.role.value, "permissions": sorted(ctx.perms)}
        out["subscription"] = None if not sub else {
            "plan": sub.plan_code, "starts_at": sub.starts_at, "ends_at": sub.ends_at,
            "estimates_used": sub.estimates_used, "estimates_limit": us["estimates_limit"],
            "materials": us["materials"], "materials_limit": us["materials_limit"],
            "users": us["users"], "users_limit": us["users_limit"]}
    return out


@router.get("/orders", response_model=list[OrderOut], summary="Заказы предприятия")
def list_orders(status: OrderStatus | None = None, q: str | None = None, limit: int = 50, offset: int = 0,
                ctx: Ctx = Depends(require_ctx)):
    items = orders.list_orders(ctx, status=status, search=q, limit=min(limit, 200), offset=offset)
    return [OrderOut(id=o.id, number=o.number, title=o.title, client_name=o.client_name, status=o.status.value,
                     cost_total=o.cost_total, price_total=o.price_total, created_at=o.created_at) for o in items]


@router.get("/orders/{order_id}", summary="Заказ с позициями")
def get_order(order_id: int, ctx: Ctx = Depends(require_ctx)):
    o = orders.get(ctx, order_id)
    return {**OrderOut(id=o.id, number=o.number, title=o.title, client_name=o.client_name, status=o.status.value,
                       cost_total=o.cost_total, price_total=o.price_total, created_at=o.created_at).model_dump(),
            "markups": {"materials": o.markup_materials, "hardware": o.markup_hardware, "works": o.markup_works,
                        "fixed": o.fixed_addition},
            "items": [{"id": i.id, "section": i.section, "kind": i.kind.value, "name": i.name, "unit": i.unit,
                       "height_mm": i.height_mm, "width_mm": i.width_mm, "pieces": i.pieces,
                       "quantity": i.quantity, "unit_cost": i.unit_cost, "cost": i.cost, "price": i.price}
                      for i in o.items]}


@router.get("/materials", summary="База материалов")
def list_materials(q: str | None = None, ctx: Ctx = Depends(require_ctx)):
    return [{"id": m.id, "kind": m.kind.value, "category": m.category, "name": m.name, "unit": m.unit,
             "price": m.price} for m in materials.list_materials(ctx, search=q)]


@router.get("/analytics/summary", summary="Финансовые показатели за период")
def analytics_summary(period: str = "month", start: date | None = None, end: date | None = None,
                      ctx: Ctx = Depends(require_ctx)):
    today = (datetime.utcnow() + TASHKENT_OFFSET).date()
    s, e = analytics.resolve_period(period, today, start, end)
    return analytics.summary(ctx, s, e)


@router.get("/payments", summary="История платежей")
def list_payments(ctx: Ctx = Depends(require_ctx)):
    ctx.require("billing.manage")
    return [{"uid": p.uid, "plan": p.plan_code, "method": p.method_code, "amount": p.amount,
             "discount_percent": p.discount_percent, "status": p.status.value, "created_at": p.created_at,
             "reject_reason": p.reject_reason} for p in payments.history(ctx.db, ctx.eid)]


@router.get("/referrals", summary="Реферальная программа")
def referral_info(ctx: Ctx = Depends(require_ctx)):
    s = referrals.summary(ctx.db, ctx.user)
    return {"links": s["links"], "qualified_in_cycle": s["qualified_in_cycle"], "discount": s["discount"],
            "total_successful": s["total_successful"],
            "history": [{"status": r.status.value, "created_at": r.created_at, "qualified_at": r.qualified_at}
                        for r in s["history"]]}
