"""Производственные затраты (BUSINESS, PRO, ENTERPRISE). «Сборка» и «Монтаж» сюда не входят —
это основные работы цеха, учитываемые через наценки (ТЗ 6.2, 8)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFound, ValidationError
from app.core.fmt import D
from app.models import Expense, ExpenseCategory
from app.services import orders as orders_svc
from app.services import tariffs
from app.services.accounts import Ctx
from app.services.audit import log

STANDARD_CATEGORIES = ["Распил", "Покраска", "Кромление", "Фрезеровка", "Присадка", "Шлифовка", "Доставка",
                       "Прочие производственные расходы"]
_FORBIDDEN_STEMS = ("сборк", "монтаж", "сборка")


def is_forbidden(name: str) -> bool:
    n = name.lower().replace("ё", "е")
    return any(stem in n for stem in _FORBIDDEN_STEMS)


def categories(ctx: Ctx) -> list[ExpenseCategory]:
    ctx.require("expenses.view")
    return list(ctx.db.scalars(select(ExpenseCategory)
                               .where(or_(ExpenseCategory.enterprise_id.is_(None),
                                          ExpenseCategory.enterprise_id == ctx.eid))
                               .order_by(ExpenseCategory.sort, ExpenseCategory.name)))


def _category(ctx: Ctx, category_id: int) -> ExpenseCategory:
    c = ctx.db.get(ExpenseCategory, category_id)
    if not c or (c.enterprise_id is not None and c.enterprise_id != ctx.eid):
        raise NotFound("Категория не найдена.")
    return c


def add_category(ctx: Ctx, name: str) -> ExpenseCategory:
    ctx.require("expenses.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "expenses")
    name = (name or "").strip()
    if not 2 <= len(name) <= 80:
        raise ValidationError("Название категории: от 2 до 80 символов.")
    if is_forbidden(name):
        raise ValidationError("«Сборка» и «Монтаж» — основные работы цеха. Они учитываются через наценки, "
                              "а не в производственных затратах.")
    if any(c.name.lower() == name.lower() for c in categories(ctx)):
        raise ValidationError("Такая категория уже есть.")
    c = ExpenseCategory(enterprise_id=ctx.eid, name=name)
    ctx.db.add(c)
    ctx.db.flush()
    return c


def add(ctx: Ctx, *, category_id: int, amount, spent_on: date | None = None, comment: str | None = None,
        order_id: int | None = None) -> Expense:
    ctx.require("expenses.edit")
    tariffs.require_feature(ctx.db, ctx.eid, "expenses")
    cat = _category(ctx, category_id)
    a = D(amount)
    if a <= 0:
        raise ValidationError("Сумма должна быть больше нуля.")
    if order_id:
        orders_svc.get(ctx, order_id)  # проверка принадлежности
    e = Expense(enterprise_id=ctx.eid, category_id=cat.id, amount=a, spent_on=spent_on or date.today(),
                comment=(comment or "").strip()[:500] or None, order_id=order_id, created_by_id=ctx.user.id)
    ctx.db.add(e)
    ctx.db.flush()
    log(ctx.db, "expense.created", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="expense", entity_id=e.id,
        amount=str(a))
    return e


def list_expenses(ctx: Ctx, date_from: date | None = None, date_to: date | None = None,
                  order_id: int | None = None, limit: int = 200) -> list[Expense]:
    ctx.require("expenses.view")
    tariffs.require_feature(ctx.db, ctx.eid, "expenses")
    q = select(Expense).where(Expense.enterprise_id == ctx.eid)
    if date_from:
        q = q.where(Expense.spent_on >= date_from)
    if date_to:
        q = q.where(Expense.spent_on <= date_to)
    if order_id:
        q = q.where(Expense.order_id == order_id)
    return list(ctx.db.scalars(q.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(limit)))


def delete(ctx: Ctx, expense_id: int) -> None:
    ctx.require("expenses.edit")
    e = ctx.db.get(Expense, expense_id)
    if not e or e.enterprise_id != ctx.eid:
        raise NotFound("Запись не найдена.")
    ctx.db.delete(e)
    log(ctx.db, "expense.deleted", actor_id=ctx.user.id, enterprise_id=ctx.eid, entity="expense", entity_id=expense_id)


def seed_standard(db: Session) -> None:
    existing = {c.name for c in db.scalars(select(ExpenseCategory).where(ExpenseCategory.enterprise_id.is_(None)))}
    for i, name in enumerate(STANDARD_CATEGORIES):
        if name not in existing:
            db.add(ExpenseCategory(enterprise_id=None, name=name, sort=i))
