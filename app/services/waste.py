"""Учёт планового/фактического расхода и отходов по заказу (PRO, ENTERPRISE).
Складской учёт и остатки в объём не входят (ТЗ 10)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.core.errors import NotFound, ValidationError
from app.core.fmt import D, money_round
from app.models import ItemKind, MaterialUsage
from app.services import orders as orders_svc
from app.services import tariffs
from app.services.accounts import Ctx
from app.services.audit import log


def _rows(ctx: Ctx, order_id: int) -> list[MaterialUsage]:
    return list(ctx.db.scalars(select(MaterialUsage).where(MaterialUsage.order_id == order_id)
                               .order_by(MaterialUsage.id)))


def plan_from_order(ctx: Ctx, order_id: int) -> list[MaterialUsage]:
    """Заполнить плановый расход из позиций заказа (материалы и фурнитура). Повторный вызов
    добавляет только недостающие позиции."""
    ctx.require("waste.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "waste")
    order = orders_svc.get(ctx, order_id)
    existing = {(r.material_id, r.name) for r in _rows(ctx, order.id)}
    agg: dict[tuple, dict] = {}
    for it in order.items:
        if it.kind == ItemKind.work:
            continue
        key = (it.material_id, it.name)
        row = agg.setdefault(key, dict(unit=it.unit, qty=Decimal(0), unit_cost=D(it.unit_cost)))
        row["qty"] += D(it.quantity)
    for (mid, name), r in agg.items():
        if (mid, name) not in existing:
            ctx.db.add(MaterialUsage(order_id=order.id, material_id=mid, name=name, unit=r["unit"],
                                     planned_qty=r["qty"], unit_cost=r["unit_cost"]))
    ctx.db.flush()
    return _rows(ctx, order.id)


def _usage(ctx: Ctx, usage_id: int) -> MaterialUsage:
    u = ctx.db.get(MaterialUsage, usage_id)
    if not u:
        raise NotFound("Запись не найдена.")
    orders_svc.get(ctx, u.order_id)  # проверка принадлежности предприятию
    return u


def set_actual(ctx: Ctx, usage_id: int, *, actual=None, waste=None, comment: str | None = None) -> MaterialUsage:
    ctx.require("waste.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "waste")
    u = _usage(ctx, usage_id)
    if actual not in (None, ""):
        a = D(actual)
        if a < 0:
            raise ValidationError("Фактический расход не может быть отрицательным.")
        u.actual_qty = a
    if waste not in (None, ""):
        w = D(waste)
        if w < 0:
            raise ValidationError("Отходы не могут быть отрицательными.")
        u.waste_qty = w
    if u.actual_qty is not None and u.waste_qty is not None and u.waste_qty > u.actual_qty:
        raise ValidationError("Отходы не могут превышать фактический расход.")
    if comment is not None:
        u.comment = comment.strip()[:300] or None
    log(ctx.db, "waste.updated", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="usage", entity_id=u.id)
    return u


def row_metrics(u: MaterialUsage) -> dict:
    planned = D(u.planned_qty)
    actual = D(u.actual_qty) if u.actual_qty is not None else None
    waste = D(u.waste_qty) if u.waste_qty is not None else None
    deviation = (actual - planned) if actual is not None else None
    return dict(
        planned=planned, actual=actual, waste=waste, deviation=deviation,
        deviation_pct=(deviation / planned * 100).quantize(Decimal("0.1")) if deviation is not None and planned else None,
        waste_pct=(waste / actual * 100).quantize(Decimal("0.1")) if waste is not None and actual else None,
        overrun_cost=money_round(deviation * D(u.unit_cost)) if deviation is not None and deviation > 0 else Decimal(0),
    )


def report(ctx: Ctx, order_id: int) -> dict:
    ctx.require("orders.view")
    tariffs.require_feature(ctx.db, ctx.eid, "waste")
    order = orders_svc.get(ctx, order_id)
    rows = [(u, row_metrics(u)) for u in _rows(ctx, order.id)]
    # Сводный % отходов взвешен по стоимости: позиции в разных единицах измерения складывать нельзя
    total_actual = sum(D(m["actual"] or 0) * D(u.unit_cost) for u, m in rows)
    total_waste = sum(D(m["waste"] or 0) * D(u.unit_cost) for u, m in rows)
    return dict(order=order, rows=rows,
                overrun_cost=sum(m["overrun_cost"] for _, m in rows),
                waste_pct=(D(total_waste) / D(total_actual) * 100).quantize(Decimal("0.1")) if total_actual else None)
