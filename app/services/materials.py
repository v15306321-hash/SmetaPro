"""База материалов и фурнитуры предприятия (ТЗ, раздел 7)."""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import LimitReached, NotFound, ValidationError
from app.core.fmt import D
from app.models import ItemKind, Material, MaterialUsage, OrderItem
from app.services import tariffs
from app.services.accounts import Ctx
from app.services.audit import log, notify

UNITS = ["шт", "м²", "м.п.", "лист", "кг", "л", "компл", "уп", "м³"]
KIND_TITLES = {ItemKind.material: "Материал", ItemKind.hardware: "Фурнитура", ItemKind.work: "Работа"}


def count_active(db: Session, enterprise_id: int) -> int:
    return db.scalar(select(func.count(Material.id)).where(Material.enterprise_id == enterprise_id,
                                                           Material.is_archived.is_(False))) or 0


def _validate(name: str, unit: str, price) -> tuple[str, str, object]:
    name = (name or "").strip()
    if not 1 <= len(name) <= 200:
        raise ValidationError("Укажите название (до 200 символов).")
    unit = (unit or "").strip()
    if not unit or len(unit) > 16:
        raise ValidationError("Укажите единицу измерения.")
    try:
        p = D(price)
    except Exception:
        raise ValidationError("Цена должна быть числом.")
    if p < 0:
        raise ValidationError("Цена не может быть отрицательной.")
    return name, unit, p


def list_materials(ctx: Ctx, kind: ItemKind | None = None, search: str | None = None,
                   category: str | None = None, include_archived: bool = False) -> list[Material]:
    ctx.require("materials.view")
    q = select(Material).where(Material.enterprise_id == ctx.eid)
    if not include_archived:
        q = q.where(Material.is_archived.is_(False))
    if kind:
        q = q.where(Material.kind == kind)
    if category:
        q = q.where(Material.category == category)
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(Material.name.ilike(like), Material.category.ilike(like)))
    return list(ctx.db.scalars(q.order_by(Material.kind, Material.category, Material.name)))


def categories(ctx: Ctx, kind: ItemKind | None = None) -> list[str]:
    q = select(Material.category).where(Material.enterprise_id == ctx.eid, Material.is_archived.is_(False))
    if kind:
        q = q.where(Material.kind == kind)
    return sorted({c for c in ctx.db.scalars(q.distinct()) if c})


def recent_prices(ctx: Ctx, category: str | None = None, kind: ItemKind | None = None, limit: int = 6) -> list:
    """Разные цены, введённые ранее (новые сначала), — для кнопок быстрого ввода цены.
    Многие материалы отличаются только цветом, поэтому цена часто повторяется."""
    q = select(Material.price).where(Material.enterprise_id == ctx.eid)
    if category:
        q = q.where(Material.category == category)
    if kind:
        q = q.where(Material.kind == kind)
    out = []
    for v in ctx.db.scalars(q.order_by(Material.id.desc()).limit(100)):
        if v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out


def recent_sheet_sizes(ctx: Ctx, limit: int = 4) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for attrs in ctx.db.scalars(select(Material.attrs).where(Material.enterprise_id == ctx.eid,
                                                              Material.attrs.is_not(None))
                                .order_by(Material.id.desc()).limit(100)):
        if attrs and attrs.get("sheet_w") and attrs.get("sheet_h"):
            size = (int(attrs["sheet_w"]), int(attrs["sheet_h"]))
            if size not in out:
                out.append(size)
        if len(out) >= limit:
            break
    return out


def count_kind(ctx: Ctx, kind: ItemKind) -> int:
    return ctx.db.scalar(select(func.count(Material.id)).where(Material.enterprise_id == ctx.eid,
                                                               Material.kind == kind)) or 0


def get(ctx: Ctx, material_id: int) -> Material:
    m = ctx.db.get(Material, material_id)
    if not m or m.enterprise_id != ctx.eid:  # изоляция предприятий
        raise NotFound("Позиция не найдена.")
    return m


def create(ctx: Ctx, *, kind: ItemKind, name: str, unit: str, price, category: str = "",
           attrs: dict | None = None) -> Material:
    ctx.require("materials.edit")
    sub = tariffs.require_active(ctx.db, ctx.eid)
    if kind not in (ItemKind.material, ItemKind.hardware):
        raise ValidationError("Тип позиции: материал или фурнитура.")
    name, unit, p = _validate(name, unit, price)
    limit = sub.plan.materials_limit
    used = count_active(ctx.db, ctx.eid)
    if used >= limit:
        notify(ctx.db, ctx.enterprise.owner_id,
               f"⚠️ Достигнут лимит базы материалов тарифа {sub.plan.title}: {limit} позиций.",
               dedup_key=f"mat-limit-{sub.id}")
        raise LimitReached(f"Достигнут лимит базы материалов ({limit} позиций на тарифе {sub.plan.title}). "
                           "Удалите неиспользуемые позиции или перейдите на тариф выше.", limit=limit)
    m = Material(enterprise_id=ctx.eid, kind=kind, name=name, unit=unit, price=p,
                 category=(category or "").strip()[:80], attrs=attrs or None)
    ctx.db.add(m)
    ctx.db.flush()
    log(ctx.db, "material.created", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="material", entity_id=m.id)
    return m


def update(ctx: Ctx, material_id: int, **fields) -> Material:
    ctx.require("materials.edit")
    tariffs.require_active(ctx.db, ctx.eid)
    m = get(ctx, material_id)
    name, unit, p = _validate(fields.get("name", m.name), fields.get("unit", m.unit), fields.get("price", m.price))
    m.name, m.unit, m.price = name, unit, p
    if "category" in fields:
        m.category = (fields["category"] or "").strip()[:80]
    if "attrs" in fields:
        m.attrs = fields["attrs"] or None
    log(ctx.db, "material.updated", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="material", entity_id=m.id)
    return m


def delete(ctx: Ctx, material_id: int) -> str:
    """Удаление с учётом зависимостей: позиция, использованная в заказах, архивируется
    (заказы и сметы сохраняют свои данные), иначе удаляется физически."""
    ctx.require("materials.edit")
    m = get(ctx, material_id)
    used = ctx.db.scalar(select(func.count(OrderItem.id)).where(OrderItem.material_id == m.id)) or 0
    used += ctx.db.scalar(select(func.count(MaterialUsage.id)).where(MaterialUsage.material_id == m.id)) or 0
    if used:
        m.is_archived = True
        result = "archived"
    else:
        ctx.db.delete(m)
        result = "deleted"
    log(ctx.db, f"material.{result}", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="material",
        entity_id=material_id)
    return result
