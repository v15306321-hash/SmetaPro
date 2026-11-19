from datetime import timedelta

import pytest

from app.core.errors import FeatureUnavailable, LimitReached, SubscriptionRequired, ValidationError
from app.models import ItemKind, OrderStatus, Role, Subscription, utcnow
from app.services import analytics, estimates, expenses, materials, orders, staff, tariffs, waste
from tests.conftest import add_member, make_enterprise


def test_no_subscription_blocks_paid_actions(db):
    ctx = make_enterprise(db, plan=None)
    with pytest.raises(SubscriptionRequired):
        orders.create(ctx, client_name="X")
    with pytest.raises(SubscriptionRequired):
        materials.create(ctx, kind=ItemKind.material, name="ЛДСП", unit="м²", price=1)


def test_materials_limit(db):
    ctx = make_enterprise(db, "START")
    plan = tariffs.get_plan(db, "START")
    plan.materials_limit = 3
    for i in range(3):
        materials.create(ctx, kind=ItemKind.material, name=f"M{i}", unit="шт", price=1)
    with pytest.raises(LimitReached):
        materials.create(ctx, kind=ItemKind.material, name="M4", unit="шт", price=1)


def test_material_delete_archives_when_used(db):
    ctx = make_enterprise(db)
    m = materials.create(ctx, kind=ItemKind.material, name="МДФ", unit="м²", price=10)
    m2 = materials.create(ctx, kind=ItemKind.material, name="Стекло", unit="м²", price=10)
    o = orders.create(ctx, client_name="Цех A")
    orders.add_item(ctx, o.id, kind=ItemKind.material, material_id=m.id, pieces=1)
    assert materials.delete(ctx, m.id) == "archived"
    assert materials.delete(ctx, m2.id) == "deleted"
    assert materials.count_active(db, ctx.eid) == 0


def test_estimate_limit_and_pdf(db):
    ctx = make_enterprise(db, "START")
    sub = tariffs.current_subscription(db, ctx.eid)
    sub.plan.estimates_limit = 2
    o = orders.create(ctx, client_name="Клиент", title="Шкаф")
    with pytest.raises(ValidationError):
        estimates.generate(ctx, o.id)  # пустой заказ
    orders.add_item(ctx, o.id, kind=ItemKind.material, name="ЛДСП", unit="м²", unit_cost=120000,
                    height_mm=2400, width_mm=600, pieces=2, section="Шкаф-купе")
    e1 = estimates.generate(ctx, o.id)
    est, data = estimates.get_file(ctx, e1.uid)
    assert data.startswith(b"%PDF") and len(data) < 200_000
    estimates.generate(ctx, o.id)
    with pytest.raises(LimitReached):
        estimates.generate(ctx, o.id)
    assert tariffs.current_subscription(db, ctx.eid).estimates_used == 2


def test_feature_gates(db):
    start = make_enterprise(db, "START")
    with pytest.raises(FeatureUnavailable) as e:
        analytics.summary(start, utcnow().date(), utcnow().date())
    assert e.value.extra["min_plan"] == "BUSINESS"
    cat = expenses.categories(start)[0]
    with pytest.raises(FeatureUnavailable):
        expenses.add(start, category_id=cat.id, amount=100)

    business = make_enterprise(db, "BUSINESS", name="Бизнес")
    expenses.add(business, category_id=cat.id, amount=100)
    o = orders.create(business, client_name="Цех A")
    with pytest.raises(FeatureUnavailable):
        orders.change_status(business, o.id, OrderStatus.in_progress)
    with pytest.raises(FeatureUnavailable):
        waste.plan_from_order(business, o.id)

    pro = make_enterprise(db, "PRO", name="Про")
    o = orders.create(pro, client_name="Цех A")
    orders.change_status(pro, o.id, OrderStatus.in_progress)
    orders.change_status(pro, o.id, OrderStatus.completed)
    hist = orders.status_history(pro, o.id)
    assert [h.to_status for h in hist] == ["new", "in_progress", "completed"]
    assert hist[-1].user_id == pro.user.id


def test_assembly_not_allowed_in_expenses(db):
    ctx = make_enterprise(db, "BUSINESS")
    for bad in ("Сборка", "монтаж кухни", "СБОРКА мебели"):
        with pytest.raises(ValidationError):
            expenses.add_category(ctx, bad)
    assert expenses.add_category(ctx, "Упаковка").id
    names = {c.name for c in expenses.categories(ctx)}
    assert "Распил" in names and "Сборка" not in names


def test_staff_limits(db):
    start = make_enterprise(db, "START")
    with pytest.raises(LimitReached):
        staff.invite(start, Role.master)
    biz = make_enterprise(db, "BUSINESS", name="Цех Б")
    add_member(db, biz, Role.manager)
    with pytest.raises(LimitReached):
        staff.invite(biz, Role.master)


def test_role_permissions(db):
    from app.core.errors import PermissionDenied
    owner = make_enterprise(db, "PRO")
    master = add_member(db, owner, Role.master)
    o = orders.create(owner, client_name="Цех A")
    with pytest.raises(PermissionDenied):
        orders.create(master, client_name="Цех B")
    orders.change_status(master, o.id, OrderStatus.in_progress)  # мастер может менять статус
    with pytest.raises(PermissionDenied):
        analytics.summary(master, utcnow().date(), utcnow().date())
    admin = add_member(db, owner, Role.admin)
    staff.change_role(owner, admin.membership.id, Role.admin, ["orders.view", "billing.manage"])
    from app.services.accounts import load_ctx
    admin = load_ctx(db, admin.user)
    assert admin.perms == {"orders.view"}  # billing.manage — только владелец


def test_subscription_expiry_keeps_data(db):
    ctx = make_enterprise(db, "START")
    m = materials.create(ctx, kind=ItemKind.material, name="ЛДСП", unit="м²", price=1)
    sub = tariffs.current_subscription(db, ctx.eid)
    sub.ends_at = utcnow() - timedelta(seconds=1)
    db.flush()
    with pytest.raises(SubscriptionRequired):
        materials.create(ctx, kind=ItemKind.material, name="Новая", unit="м²", price=1)
    assert [x.id for x in materials.list_materials(ctx)] == [m.id]  # данные доступны для просмотра
    tariffs.activate(db, ctx.eid, "START", None)
    materials.create(ctx, kind=ItemKind.material, name="Новая", unit="м²", price=1)


def test_renewal_and_plan_change(db):
    ctx = make_enterprise(db, plan=None)
    s1 = tariffs.activate(db, ctx.eid, "START", None)
    s1.estimates_used = 5
    s2 = tariffs.activate(db, ctx.eid, "START", None)   # продление — новый период после текущего
    assert s2.starts_at == s1.ends_at
    assert tariffs.current_subscription(db, ctx.eid).id == s1.id
    s3 = tariffs.activate(db, ctx.eid, "PRO", None)     # смена тарифа — сразу
    cur = tariffs.current_subscription(db, ctx.eid)
    assert cur.id == s3.id and cur.estimates_used == 0 and cur.plan.estimates_limit == 100
    db.refresh(s2)
    assert s1.superseded_at and s2.superseded_at


def test_waste_report(db):
    ctx = make_enterprise(db, "PRO")
    o = orders.create(ctx, client_name="Цех A")
    orders.add_item(ctx, o.id, kind=ItemKind.material, name="ЛДСП", unit="м²", unit_cost=100, quantity=10)
    orders.add_item(ctx, o.id, kind=ItemKind.work, name="Сборка", unit="услуга", unit_cost=100)
    rows = waste.plan_from_order(ctx, o.id)
    assert len(rows) == 1 and rows[0].planned_qty == 10
    waste.set_actual(ctx, rows[0].id, actual=12, waste=3)
    rep = waste.report(ctx, o.id)
    _, m = rep["rows"][0]
    assert m["deviation"] == 2 and m["deviation_pct"] == 20 and m["waste_pct"] == 25
    assert rep["overrun_cost"] == 200
    assert len(waste.plan_from_order(ctx, o.id)) == 1  # повторно не дублирует


def test_analytics_summary(db):
    ctx = make_enterprise(db, "BUSINESS")
    o = orders.create(ctx, client_name="Цех A")
    orders.add_item(ctx, o.id, kind=ItemKind.material, name="ЛДСП", unit="шт", unit_cost=1000, quantity=10)
    cat = expenses.categories(ctx)[0]
    expenses.add(ctx, category_id=cat.id, amount=500)
    today = (utcnow() + timedelta(hours=5)).date()
    s = analytics.summary(ctx, *analytics.resolve_period("month", today))
    assert s["revenue"] == 13000 and s["cost"] == 10000 and s["gross"] == 3000
    assert s["expenses"] == 500 and s["result"] == 2500 and s["orders"] == 1
    series = analytics.monthly_series(ctx, 3, today)
    assert series["revenue"][-1] == 13000.0


def test_tenant_isolation(db):
    from app.core.errors import NotFound
    a = make_enterprise(db, "PRO", name="Цех A")
    b = make_enterprise(db, "PRO", name="Цех B")
    m = materials.create(a, kind=ItemKind.material, name="Секрет", unit="шт", price=1)
    o = orders.create(a, client_name="Клиент A")
    with pytest.raises(NotFound):
        orders.get(b, o.id)
    with pytest.raises(NotFound):
        materials.get(b, m.id)
    ob = orders.create(b, client_name="Цех B")
    with pytest.raises(NotFound):  # нельзя подставить чужой материал в свой заказ
        orders.add_item(b, ob.id, kind=ItemKind.material, material_id=m.id)
    assert orders.list_orders(b) == [ob]
    orders.add_item(a, o.id, kind=ItemKind.material, material_id=m.id)
    est = estimates.generate(a, o.id)
    with pytest.raises(NotFound):
        estimates.get_file(b, est.uid)
    _ = Subscription


def test_logo_in_pdf_stored_in_db(db):
    import io

    from PIL import Image as PILImage

    from app.models import StoredFile
    from app.services import accounts
    buf = io.BytesIO()
    PILImage.new("RGB", (120, 40), (169, 135, 111)).save(buf, "PNG")
    start = make_enterprise(db, "START", name="Без логотипа")
    with pytest.raises(FeatureUnavailable):
        accounts.set_logo(start, buf.getvalue(), "")
    ctx = make_enterprise(db, "BUSINESS", name="С логотипом")
    accounts.set_logo(ctx, buf.getvalue(), "")
    first = ctx.enterprise.logo_path
    accounts.set_logo(ctx, buf.getvalue(), "")          # замена — старый файл удаляется
    assert db.get(StoredFile, first) is None
    o = orders.create(ctx, client_name="Клиент")
    orders.add_item(ctx, o.id, kind=ItemKind.material, name="ЛДСП", unit="шт", unit_cost=100, quantity=1)
    plain = estimates.generate(ctx, o.id)
    _, data = estimates.get_file(ctx, plain.uid)
    assert data.startswith(b"%PDF") and b"/Subtype /Image" in data   # логотип встроен в PDF
    orders.delete(ctx, o.id)
    assert db.query(StoredFile).filter_by(kind="estimate").count() == 0  # PDF удалены вместе с заказом
