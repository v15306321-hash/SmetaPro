"""Формирование PDF-смет. Каждая генерация списывает одну единицу лимита тарифа."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.errors import NotFound, ValidationError
from app.core.fmt import D, local_dt, money_round, qty
from app.core.security import short_code
from app.models import Estimate, Order, utcnow
from app.pdf.estimate import PdfData, PdfLine, render
from app.services import orders as orders_svc
from app.services import tariffs
from app.services.accounts import Ctx
from app.services.audit import log, notify
from app.services import files


def build_pdf_data(ctx: Ctx, order: Order, uid: str, created_at: datetime, with_logo: bool) -> PdfData:
    ent = ctx.enterprise
    sections = []
    for name, items in orders_svc.grouped_items(order):
        lines = []
        for it in items:
            size = ""
            if it.height_mm and it.width_mm:
                size = f"{it.height_mm}×{it.width_mm}"
            elif it.width_mm or it.height_mm:
                size = str(it.width_mm or it.height_mm)
            q = D(it.quantity)
            unit_price = money_round(D(it.price) / q) if q else Decimal(0)
            lines.append(PdfLine(name=it.name, size=size, pieces=qty(it.pieces), quantity=qty(it.quantity),
                                 unit=it.unit, unit_price=unit_price, total=D(it.price)))
        sections.append((name, lines))
    logo = None
    if with_logo and ent.logo_path:
        try:
            logo = files.load(ctx.db, ent.logo_path, ctx.eid).data
        except Exception:
            logo = None
    return PdfData(uid=uid, created_at=local_dt(created_at), company_name=ent.name, company_phone=ent.phone,
                   company_address=ent.address, company_email=ent.email, company_tax_id=ent.tax_id,
                   logo=logo, order_number=order.number, order_title=order.title,
                   client_name=order.client_name, client_phone=order.client_phone,
                   client_address=order.client_address, sections=sections,
                   extra_label=ent.fixed_addition_label or "Дополнительные услуги",
                   extra_amount=D(order.fixed_addition), total=D(order.price_total))


def generate(ctx: Ctx, order_id: int) -> Estimate:
    ctx.require("estimates.create")
    db = ctx.db
    sub = tariffs.require_active(db, ctx.eid)
    order = orders_svc.get(ctx, order_id)
    if not order.items:
        raise ValidationError("В заказе нет позиций — добавьте материалы, фурнитуру или работы.")
    orders_svc.recalc(order)
    tariffs.consume_estimate(db, sub)  # атомарно, бросает LimitReached

    now = utcnow()
    uid = f"EST-{local_dt(now):%y%m%d}-{short_code(5)}"
    with_logo = bool(sub.plan.features.get("pdf_logo"))
    pdf = render(build_pdf_data(ctx, order, uid, now, with_logo))
    key = files.save_pdf(db, pdf, ctx.eid)
    est = Estimate(uid=uid, enterprise_id=ctx.eid, order_id=order.id, subscription_id=sub.id,
                   total=order.price_total, file_path=key, created_by_id=ctx.user.id, created_at=now)
    db.add(est)
    db.flush()
    log(db, "estimate.generated", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="estimate", entity_id=uid,
        order_id=order.id, total=str(order.price_total))
    left = sub.plan.estimates_limit - sub.estimates_used
    if left == 0:
        notify(db, ctx.enterprise.owner_id,
               f"⚠️ Использованы все генерации смет тарифа {sub.plan.title} ({sub.plan.estimates_limit}). "
               "Лимит обновится с новым оплаченным периодом.", dedup_key=f"est-limit-{sub.id}")
    elif left <= max(2, sub.plan.estimates_limit // 10):
        notify(db, ctx.enterprise.owner_id, f"ℹ️ Осталось генераций смет: {left}.",
               dedup_key=f"est-low-{sub.id}-{left}")
    return est


def list_for_order(ctx: Ctx, order_id: int) -> list[Estimate]:
    order = orders_svc.get(ctx, order_id)
    return list(ctx.db.scalars(select(Estimate).where(Estimate.order_id == order.id)
                               .order_by(Estimate.created_at.desc())))


def recent(ctx: Ctx, limit: int = 10) -> list[Estimate]:
    ctx.require("orders.view")
    return list(ctx.db.scalars(select(Estimate).where(Estimate.enterprise_id == ctx.eid)
                               .order_by(Estimate.created_at.desc()).limit(limit)))


def get_file(ctx: Ctx, uid: str) -> tuple[Estimate, bytes]:
    ctx.require("orders.view")
    est = ctx.db.scalar(select(Estimate).where(Estimate.uid == uid))
    if not est or est.enterprise_id != ctx.eid:
        raise NotFound("Смета не найдена.")
    return est, files.load(ctx.db, est.file_path, ctx.eid).data
