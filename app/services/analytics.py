"""Финансовая аналитика (BUSINESS, PRO, ENTERPRISE). Методика — docs/CALCULATION.md.

Термины:
  Выручка (расчётная)        = Σ итоговых цен заказов за период
  Себестоимость заказов      = Σ (материалы + фурнитура + работы цеха) по себестоимости
  Валовая прибыль            = Выручка − Себестоимость (= доход от наценок и фиксированных сумм)
  Производственные затраты   = Σ записей раздела «Производственные затраты» за период
  Оплата рабочим             = Σ оплаты сотрудникам, назначенным на заказы
  Расчётный фин. результат   = Валовая прибыль − Оплата рабочим − Производственные затраты
Это НЕ фактическая чистая прибыль: налоги, аренда, зарплаты и прочие обязательства не учитываются.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.errors import ValidationError
from app.core.fmt import D, TASHKENT_OFFSET
from app.models import Expense, ExpenseCategory, ItemKind, Material, Order, OrderItem, OrderStatus
from app.services import tariffs
from app.services.accounts import Ctx

PERIODS = {"day": "День", "week": "Неделя", "month": "Месяц", "year": "Год", "custom": "Период"}
MONTHS = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def resolve_period(kind: str, today: date, start: date | None = None, end: date | None = None) -> tuple[date, date]:
    """Возвращает включительный диапазон локальных дат."""
    if kind == "day":
        return today, today
    if kind == "week":
        s = today - timedelta(days=today.weekday())
        return s, s + timedelta(days=6)
    if kind == "month":
        s = today.replace(day=1)
        nxt = (s + timedelta(days=32)).replace(day=1)
        return s, nxt - timedelta(days=1)
    if kind == "year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    if kind == "custom":
        if not start or not end or start > end:
            raise ValidationError("Укажите корректный диапазон дат.")
        if (end - start).days > 3 * 366:
            raise ValidationError("Диапазон не может превышать 3 года.")
        return start, end
    raise ValidationError("Неизвестный период.")


def _utc_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    s = datetime.combine(start, datetime.min.time()) - TASHKENT_OFFSET
    e = datetime.combine(end + timedelta(days=1), datetime.min.time()) - TASHKENT_OFFSET
    return s, e


def _order_filter(ctx: Ctx, start: date, end: date, completed_only: bool):
    s, e = _utc_bounds(start, end)
    conds = [Order.enterprise_id == ctx.eid, Order.created_at >= s, Order.created_at < e,
             Order.price_total > 0]
    if completed_only:
        conds.append(Order.status == OrderStatus.completed)
    return conds


def summary(ctx: Ctx, start: date, end: date, completed_only: bool = False) -> dict:
    ctx.require("analytics.view")
    tariffs.require_feature(ctx.db, ctx.eid, "analytics")
    db = ctx.db
    conds = _order_filter(ctx, start, end, completed_only)
    row = db.execute(select(func.count(Order.id), func.coalesce(func.sum(Order.price_total), 0),
                            func.coalesce(func.sum(Order.cost_materials), 0),
                            func.coalesce(func.sum(Order.cost_hardware), 0),
                            func.coalesce(func.sum(Order.cost_works), 0),
                            func.coalesce(func.sum(Order.cost_total), 0),
                            func.coalesce(func.sum(Order.workers_cost), 0)).where(*conds)).one()
    count, revenue, c_mat, c_hw, c_work, cost = row[0], D(row[1]), D(row[2]), D(row[3]), D(row[4]), D(row[5])
    workers = D(row[6])
    expenses = D(db.scalar(select(func.coalesce(func.sum(Expense.amount), 0))
                           .where(Expense.enterprise_id == ctx.eid, Expense.spent_on >= start,
                                  Expense.spent_on <= end)))
    gross = revenue - cost          # стоимость работы цеха (наценка) + доплаты
    result = gross - workers - expenses
    pct = lambda a, b: (D(a) / D(b) * 100).quantize(Decimal("0.1")) if b else Decimal(0)  # noqa: E731
    return dict(
        start=start, end=end, orders=count, revenue=revenue, cost=cost,
        cost_materials=c_mat, cost_hardware=c_hw, cost_works=c_work,
        materials_and_hardware=c_mat + c_hw, gross=gross, markup_income=gross,
        expenses=expenses, workers=workers, result=result, gross_margin=pct(gross, revenue), result_margin=pct(result, revenue),
        avg_check=(revenue / count).quantize(Decimal("1")) if count else Decimal(0),
    )


def previous_range(start: date, end: date) -> tuple[date, date]:
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def monthly_series(ctx: Ctx, months: int = 7, today: date | None = None) -> dict:
    """Помесячно: выручка, себестоимость + произв. затраты, результат — для графика трендов."""
    ctx.require("analytics.view")
    tariffs.require_feature(ctx.db, ctx.eid, "analytics")
    today = today or (datetime.utcnow() + TASHKENT_OFFSET).date()
    labels, revenue, costs, result = [], [], [], []
    first = today.replace(day=1)
    starts = []
    for i in range(months - 1, -1, -1):
        y, m = first.year, first.month - i
        while m <= 0:
            m += 12
            y -= 1
        starts.append(date(y, m, 1))
    for s in starts:
        e = (s + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        sm = summary(ctx, s, e)
        labels.append(f"{MONTHS[s.month - 1]} {str(s.year)[2:]}")
        revenue.append(float(sm["revenue"]))
        costs.append(float(sm["cost"] + sm["expenses"] + sm["workers"]))
        result.append(float(sm["result"]))
    return dict(labels=labels, revenue=revenue, costs=costs, result=result)


def daily_series(ctx: Ctx, start: date, end: date) -> dict:
    """Для спарклайнов KPI-карточек."""
    s, e = _utc_bounds(start, end)
    days = (end - start).days + 1
    step = max(1, days // 31 + (1 if days % 31 else 0)) if days > 31 else 1
    rows = ctx.db.execute(select(Order.created_at, Order.price_total, Order.cost_materials, Order.cost_hardware,
                                 Order.cost_works)
                          .where(Order.enterprise_id == ctx.eid, Order.created_at >= s, Order.created_at < e)).all()
    buckets = (days + step - 1) // step
    rev, mat, work = [0.0] * buckets, [0.0] * buckets, [0.0] * buckets
    for created, price, cm, ch, cw in rows:
        idx = min(buckets - 1, ((created + TASHKENT_OFFSET).date() - start).days // step)
        rev[idx] += float(price or 0)
        mat[idx] += float((cm or 0) + (ch or 0))
        work[idx] += float(cw or 0)
    return dict(revenue=rev, materials=mat, works=work)


def materials_breakdown(ctx: Ctx, start: date, end: date) -> list[tuple[str, float]]:
    """Затраты на материалы/фурнитуру по категориям базы."""
    s, e = _utc_bounds(start, end)
    q = (select(func.coalesce(Material.category, ""), OrderItem.kind, func.sum(OrderItem.cost))
         .join(Order, Order.id == OrderItem.order_id)
         .outerjoin(Material, Material.id == OrderItem.material_id)
         .where(Order.enterprise_id == ctx.eid, Order.created_at >= s, Order.created_at < e,
                OrderItem.kind != ItemKind.work)
         .group_by(Material.category, OrderItem.kind))
    agg: dict[str, float] = {}
    for cat, kind, total in ctx.db.execute(q):
        label = cat or ("Фурнитура" if kind == ItemKind.hardware else "Прочие материалы")
        agg[label] = agg.get(label, 0) + float(total or 0)
    return sorted(agg.items(), key=lambda x: -x[1])[:8]


def expenses_breakdown(ctx: Ctx, start: date, end: date) -> list[tuple[str, float]]:
    q = (select(ExpenseCategory.name, func.sum(Expense.amount))
         .join(ExpenseCategory, ExpenseCategory.id == Expense.category_id)
         .where(Expense.enterprise_id == ctx.eid, Expense.spent_on >= start, Expense.spent_on <= end)
         .group_by(ExpenseCategory.name))
    return sorted(((n, float(t or 0)) for n, t in ctx.db.execute(q)), key=lambda x: -x[1])


def top_orders(ctx: Ctx, start: date, end: date, limit: int = 6) -> list[Order]:
    conds = _order_filter(ctx, start, end, False)
    return list(ctx.db.scalars(select(Order).where(*conds).order_by(Order.price_total.desc()).limit(limit)))


def margin_by_section(ctx: Ctx, start: date, end: date) -> list[tuple[str, float]]:
    """Маржинальность (%) по изделиям: прибыль цеха (работа цеха + доплата − оплата рабочим) к цене.

    Прибыль заказа делится между изделиями пропорционально их себестоимости.
    """
    from collections import defaultdict
    s, e = _utc_bounds(start, end)
    rows = ctx.db.scalars(select(Order).where(Order.enterprise_id == ctx.eid, Order.created_at >= s,
                                              Order.created_at < e))
    price, profit = defaultdict(float), defaultdict(float)
    for o in rows:
        cost_total = float(o.cost_total or 0)
        if not o.items or cost_total <= 0:
            continue
        order_profit, order_price = float(o.profit), float(o.price_total or 0)
        for it in o.items:
            share = float(it.cost or 0) / cost_total
            key = it.section or o.title or "Без раздела"
            price[key] += order_price * share
            profit[key] += order_profit * share
    out = [(k, round(profit[k] / price[k] * 100, 1)) for k in price if price[k]]
    return sorted(out, key=lambda x: -x[1])[:6]
