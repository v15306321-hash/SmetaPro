"""Заказы и калькулятор стоимости (ТЗ, раздел 6).

Формула (предложенная, п. 26 ТЗ — подлежит согласованию; описана в docs/CALCULATION.md):

  количество позиции:
    м²   при заданных высоте и ширине: В(мм) × Ш(мм) / 1 000 000 × кол-во штук
    м.п. при заданной длине (ширине): Ш(мм) / 1000 × кол-во штук
    иначе: количество, указанное пользователем
  себестоимость позиции = количество × цена за единицу (из базы предприятия)
  цена позиции для клиента = себестоимость × (1 + наценка вида позиции / 100)
     вид: материалы | фурнитура | работы цеха (в т.ч. сборка и монтаж)
  итог заказа = Σ цен позиций + фиксированная дополнительная сумма
  доход от наценок = итог − Σ себестоимости
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationError
from app.core.fmt import D, money_round
from app.models import (Enterprise, ItemKind, Order, OrderItem, OrderStatus, OrderStatusHistory, utcnow)
from app.services import materials as materials_svc
from app.services import tariffs
from app.services.accounts import Ctx
from app.services.audit import log, notify

STATUS_TITLES = {
    OrderStatus.new: "Новый",
    OrderStatus.in_progress: "В работе",
    OrderStatus.manufactured: "Изготовлен",
    OrderStatus.completed: "Завершён",
}
WORK_UNITS = ["услуга", "час", "шт", "м²", "м.п.", "компл"]
MAX_QTY = Decimal("1000000")


def compute_quantity(unit: str, pieces, height_mm: int | None, width_mm: int | None, quantity=None) -> Decimal:
    pieces = D(pieces) if pieces not in (None, "") else Decimal("1")
    if pieces <= 0:
        raise ValidationError("Количество должно быть больше нуля.")
    if unit == "м²" and height_mm and width_mm:
        q = Decimal(height_mm) * Decimal(width_mm) / Decimal(1_000_000) * pieces
    elif unit == "м.п." and (width_mm or height_mm) and quantity in (None, ""):
        q = Decimal(width_mm or height_mm) / Decimal(1000) * pieces
    elif quantity not in (None, ""):
        q = D(quantity)
    else:
        q = pieces
    if q <= 0 or q > MAX_QTY:
        raise ValidationError("Недопустимое количество.")
    return q.quantize(Decimal("0.001"))


def markup_for(order: Order, kind: ItemKind) -> Decimal:
    return {ItemKind.material: order.markup_materials, ItemKind.hardware: order.markup_hardware,
            ItemKind.work: order.markup_works}[kind]


def recalc(order: Order) -> Order:
    totals = {ItemKind.material: Decimal(0), ItemKind.hardware: Decimal(0), ItemKind.work: Decimal(0)}
    price_sum = Decimal(0)
    for it in order.items:
        it.cost = money_round(D(it.quantity) * D(it.unit_cost))
        it.price = money_round(it.cost * (1 + D(markup_for(order, it.kind)) / 100))
        totals[it.kind] += it.cost
        price_sum += it.price
    order.cost_materials = totals[ItemKind.material]
    order.cost_hardware = totals[ItemKind.hardware]
    order.cost_works = totals[ItemKind.work]
    order.cost_total = sum(totals.values())
    order.price_total = money_round(price_sum + D(order.fixed_addition)) if order.items else Decimal(0)
    return order


# ---------------------------------------------------------------- CRUD

def _next_number(db: Session, enterprise_id: int) -> int:
    ent = db.scalars(select(Enterprise).where(Enterprise.id == enterprise_id).with_for_update()).one()
    ent.order_seq = (ent.order_seq or 0) + 1
    return ent.order_seq


def create(ctx: Ctx, *, title: str = "", client_name: str = "", client_phone: str | None = None,
           client_address: str | None = None, comment: str | None = None) -> Order:
    ctx.require("orders.edit")
    tariffs.require_active(ctx.db, ctx.eid)
    ent = ctx.enterprise
    o = Order(enterprise_id=ctx.eid, number=_next_number(ctx.db, ctx.eid), title=(title or "").strip()[:200],
              client_name=(client_name or "").strip()[:200], client_phone=(client_phone or "").strip()[:32] or None,
              client_address=(client_address or "").strip()[:300] or None, comment=comment,
              markup_materials=ent.markup_materials, markup_hardware=ent.markup_hardware,
              markup_works=ent.markup_works, fixed_addition=ent.fixed_addition, created_by_id=ctx.user.id)
    ctx.db.add(o)
    ctx.db.flush()
    _history(ctx, o, None, OrderStatus.new)
    log(ctx.db, "order.created", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="order", entity_id=o.id)
    if ctx.user.id != ent.owner_id:
        notify(ctx.db, ent.owner_id, f"📋 {ctx.user.full_name or 'Сотрудник'} создал заказ №{o.number}"
                                     f" {o.client_name}".strip())
    return o


def list_orders(ctx: Ctx, status: OrderStatus | None = None, search: str | None = None,
                limit: int = 50, offset: int = 0) -> list[Order]:
    ctx.require("orders.view")
    q = select(Order).where(Order.enterprise_id == ctx.eid)
    if status:
        q = q.where(Order.status == status)
    if search:
        like = f"%{search.strip()}%"
        q = q.where(Order.client_name.ilike(like) | Order.title.ilike(like))
    return list(ctx.db.scalars(q.order_by(Order.created_at.desc()).limit(limit).offset(offset)))


def count_orders(ctx: Ctx) -> int:
    return ctx.db.scalar(select(func.count(Order.id)).where(Order.enterprise_id == ctx.eid)) or 0


def get(ctx: Ctx, order_id: int) -> Order:
    ctx.require("orders.view")
    o = ctx.db.get(Order, order_id)
    if not o or o.enterprise_id != ctx.eid:
        raise NotFound("Заказ не найден.")
    return o


def update_info(ctx: Ctx, order_id: int, **fields) -> Order:
    ctx.require("orders.edit")
    tariffs.require_active(ctx.db, ctx.eid)
    o = get(ctx, order_id)
    for k in ("title", "client_name", "client_phone", "client_address", "comment"):
        if k in fields and fields[k] is not None:
            setattr(o, k, str(fields[k]).strip() or (None if k != "title" and k != "client_name" else ""))
    log(ctx.db, "order.updated", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="order", entity_id=o.id)
    return o


def set_markups(ctx: Ctx, order_id: int, *, materials=None, hardware=None, works=None, fixed=None) -> Order:
    """Корректировка наценок конкретного заказа до формирования сметы."""
    ctx.require("orders.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "custom_markups")
    o = get(ctx, order_id)
    for attr, val in (("markup_materials", materials), ("markup_hardware", hardware), ("markup_works", works)):
        if val is not None:
            v = D(val)
            if not 0 <= v <= 1000:
                raise ValidationError("Наценка должна быть от 0 до 1000%.")
            setattr(o, attr, v)
    if fixed is not None:
        if D(fixed) < 0:
            raise ValidationError("Сумма не может быть отрицательной.")
        o.fixed_addition = D(fixed)
    recalc(o)
    log(ctx.db, "order.markups", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="order", entity_id=o.id)
    return o


def add_item(ctx: Ctx, order_id: int, *, kind: ItemKind, material_id: int | None = None, name: str | None = None,
             unit: str | None = None, unit_cost=None, pieces=1, height_mm: int | None = None,
             width_mm: int | None = None, quantity=None, section: str = "") -> OrderItem:
    ctx.require("orders.edit")
    tariffs.require_active(ctx.db, ctx.eid)
    o = get(ctx, order_id)
    if material_id:
        m = materials_svc.get(ctx, material_id)  # проверка принадлежности предприятию
        kind = m.kind
        name = name or m.name
        unit = unit or m.unit
        unit_cost = m.price if unit_cost in (None, "") else unit_cost
    name = (name or "").strip()
    if not name:
        raise ValidationError("Укажите наименование позиции.")
    if not unit:
        raise ValidationError("Укажите единицу измерения.")
    cost = D(unit_cost)
    if cost < 0:
        raise ValidationError("Цена не может быть отрицательной.")
    for v in (height_mm, width_mm):
        if v is not None and not 0 < int(v) <= 100_000:
            raise ValidationError("Размеры указываются в миллиметрах (до 100 000).")
    item = OrderItem(order_id=o.id, kind=kind, material_id=material_id, name=name[:200], unit=unit[:16],
                     height_mm=height_mm, width_mm=width_mm, pieces=D(pieces or 1),
                     quantity=compute_quantity(unit, pieces, height_mm, width_mm, quantity),
                     unit_cost=cost, section=(section or "").strip()[:120])
    o.items.append(item)
    recalc(o)
    ctx.db.flush()
    return item


def remove_item(ctx: Ctx, order_id: int, item_id: int) -> Order:
    ctx.require("orders.edit")
    tariffs.require_active(ctx.db, ctx.eid)
    o = get(ctx, order_id)
    item = next((i for i in o.items if i.id == item_id), None)
    if not item:
        raise NotFound("Позиция не найдена.")
    o.items.remove(item)
    recalc(o)
    return o


def delete(ctx: Ctx, order_id: int) -> None:
    ctx.require("orders.edit")
    o = get(ctx, order_id)
    from app.models import Estimate
    from app.services import files
    for key in ctx.db.scalars(select(Estimate.file_path).where(Estimate.order_id == o.id)):
        files.delete(ctx.db, key)  # PDF хранятся в БД — удаляем вместе с заказом
    ctx.db.delete(o)
    log(ctx.db, "order.deleted", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="order", entity_id=order_id)


def _history(ctx: Ctx, o: Order, frm: OrderStatus | None, to: OrderStatus) -> None:
    ctx.db.add(OrderStatusHistory(order_id=o.id, from_status=frm.value if frm else None, to_status=to.value,
                                  user_id=ctx.user.id))


def change_status(ctx: Ctx, order_id: int, status: OrderStatus) -> Order:
    ctx.require("orders.status")
    tariffs.require_feature(ctx.db, ctx.eid, "order_statuses")
    o = get(ctx, order_id)
    if o.status == status:
        return o
    prev = o.status
    o.status = status
    o.updated_at = utcnow()
    _history(ctx, o, prev, status)
    log(ctx.db, "order.status", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="order", entity_id=o.id,
        **{"from": prev.value, "to": status.value})
    if ctx.user.id != ctx.enterprise.owner_id:
        notify(ctx.db, ctx.enterprise.owner_id,
               f"🔄 Заказ №{o.number}: {STATUS_TITLES[prev]} → {STATUS_TITLES[status]} ({ctx.user.full_name})")
    return o


def status_history(ctx: Ctx, order_id: int) -> list[OrderStatusHistory]:
    o = get(ctx, order_id)
    return list(ctx.db.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id)
                               .order_by(OrderStatusHistory.created_at)))


def grouped_items(order: Order) -> list[tuple[str, list[OrderItem]]]:
    groups: dict[str, list[OrderItem]] = {}
    for it in order.items:
        groups.setdefault(it.section or "Изделие", []).append(it)
    return list(groups.items())
