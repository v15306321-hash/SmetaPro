from decimal import Decimal

import pytest

from app.core.errors import ValidationError
from app.core.fmt import amount_in_words, money
from app.models import ItemKind
from app.services import accounts, materials, orders
from tests.conftest import make_enterprise


def test_quantity_rules():
    assert orders.compute_quantity("м²", 2, 600, 400) == Decimal("0.480")
    assert orders.compute_quantity("м.п.", 3, None, 2500) == Decimal("7.500")
    assert orders.compute_quantity("шт", 4, None, None) == Decimal("4.000")
    assert orders.compute_quantity("лист", 1, None, None, quantity="2,5") == Decimal("2.500")
    with pytest.raises(ValidationError):
        orders.compute_quantity("шт", 0, None, None)


def test_order_totals_workshop_fee(db):
    """Себестоимость без наценок; наценка — «стоимость работы цеха» на общую сумму; рабочие — расход цеха."""
    from app.services import employees
    ctx = make_enterprise(db, "START")
    accounts.set_markups(ctx, works=10, fixed=50000)
    acryl = materials.create(ctx, kind=ItemKind.material, name="Акрил Темный асфальт", unit="лист", price=660000,
                             category="Корпусное покрытие", attrs={"sheet_w": 1220, "sheet_h": 2440})
    hinge = materials.create(ctx, kind=ItemKind.hardware, name="Петля Blum", unit="шт", price=25000)
    o = orders.create(ctx, title="Кухня", client_name="Иван")
    assert o.markup_materials == 0 and o.markup_works == Decimal("10")
    orders.add_item(ctx, o.id, kind=ItemKind.material, material_id=acryl.id, quantity=3, section="Кухня")
    orders.add_item(ctx, o.id, kind=ItemKind.hardware, material_id=hinge.id, quantity=10, section="Кухня")
    orders.add_item(ctx, o.id, kind=ItemKind.work, name="Распил", unit="услуга", unit_cost=150000, quantity=1)
    db.flush()
    assert o.cost_total == Decimal("2380000")                       # 1.980.000 + 250.000 + 150.000
    assert all(it.price == it.cost for it in o.items)               # без наценок на позиции
    assert o.workshop_fee == Decimal("238000")                      # 10 % от общей суммы
    assert o.price_total == Decimal("2380000") + Decimal("238000") + Decimal("50000")
    # рабочие: процент от итога и фиксированная сумма; клиенту не добавляются
    master = employees.create(ctx, name="Бахтиёр", position="Мастер", pay_type="percent", pay_rate=5)
    helper = employees.create(ctx, name="Азиз", position="Помощник мастера", pay_type="fixed", pay_rate=100000)
    total_before = o.price_total
    orders.add_worker(ctx, o.id, master.id)
    orders.add_worker(ctx, o.id, helper.id)
    assert o.price_total == total_before
    assert o.workers_cost == Decimal("133400") + Decimal("100000")  # 5 % от 2.668.000
    assert o.profit == Decimal("288000") - Decimal("233400")
    # смена наценки заказа пересчитывает и процент рабочего
    orders.set_markups(ctx, o.id, works=0)
    assert o.price_total == Decimal("2430000") and o.workers[0].amount == Decimal("121500")
    orders.remove_worker(ctx, o.id, o.workers[1].id)
    assert o.workers_cost == Decimal("121500")


def test_markup_snapshot_is_per_order(db):
    ctx = make_enterprise(db)
    accounts.set_markups(ctx, works=15)
    o = orders.create(ctx, client_name="A")
    accounts.set_markups(ctx, works=99)
    assert o.markup_works == Decimal("15")
    o2 = orders.create(ctx, client_name="B")
    assert o2.markup_works == Decimal("99")
    assert o2.number == o.number + 1


def test_money_format_and_words():
    assert money(1234567.5) == "1.234.567,50\u00a0сум"
    assert money(660000) == "660.000\u00a0сум"
    assert amount_in_words(1_201_021) == "Один миллион двести одна тысяча двадцать один сум 00 тийин"
    assert amount_in_words(2000) == "Две тысячи сум 00 тийин"
    assert amount_in_words(0) == "Ноль сум 00 тийин"


def test_free_input_parsing():
    from decimal import Decimal

    from app.core.fmt import parse_money, parse_qty
    for text, value in [("660.000", 660000), ("660 000 сум", 660000), ("660000", 660000), ("440,000", 440000),
                        ("1.250.000,50", Decimal("1250000.50")), ("99,5", Decimal("99.5"))]:
        assert parse_money(text) == Decimal(value), text
    for text, value in [("3", 3), ("1,5", Decimal("1.5")), ("2.5 листа", Decimal("2.5"))]:
        assert parse_qty(text) == Decimal(value), text
    with pytest.raises(ValueError):
        parse_money("дорого")
