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


def test_order_totals_with_markups(db):
    ctx = make_enterprise(db, "START")
    accounts.set_markups(ctx, materials=30, hardware=20, works=10, fixed=50000)
    ldsp = materials.create(ctx, kind=ItemKind.material, name="ЛДСП Egger 16 мм", unit="м²", price=100000,
                            category="ЛДСП")
    hinge = materials.create(ctx, kind=ItemKind.hardware, name="Петля Blum", unit="шт", price=25000)
    o = orders.create(ctx, title="Кухня", client_name="Иван")
    assert o.markup_materials == Decimal("30")
    orders.add_item(ctx, o.id, kind=ItemKind.material, material_id=ldsp.id, height_mm=2000, width_mm=1000,
                    pieces=2, section="Кухня")                                   # 4 м² × 100 000 = 400 000
    orders.add_item(ctx, o.id, kind=ItemKind.hardware, material_id=hinge.id, pieces=10, section="Кухня")  # 250 000
    orders.add_item(ctx, o.id, kind=ItemKind.work, name="Сборка и монтаж", unit="услуга", unit_cost=300000)
    db.flush()
    assert o.cost_materials == Decimal("400000.00")
    assert o.cost_hardware == Decimal("250000.00")
    assert o.cost_works == Decimal("300000.00")
    assert o.cost_total == Decimal("950000.00")
    # 400k×1.3 + 250k×1.2 + 300k×1.1 + 50k
    assert o.price_total == Decimal("520000") + Decimal("300000") + Decimal("330000") + Decimal("50000")
    assert o.markup_income == o.price_total - o.cost_total

    # корректировка наценки конкретного заказа
    orders.set_markups(ctx, o.id, materials=0)
    assert o.price_total == Decimal("400000") + Decimal("300000") + Decimal("330000") + Decimal("50000")

    # удаление позиции пересчитывает итог
    orders.remove_item(ctx, o.id, o.items[-1].id)
    assert o.cost_works == 0


def test_markup_snapshot_is_per_order(db):
    ctx = make_enterprise(db)
    o = orders.create(ctx, client_name="A")
    accounts.set_markups(ctx, materials=99)
    assert o.markup_materials == Decimal("30")
    o2 = orders.create(ctx, client_name="B")
    assert o2.markup_materials == Decimal("99")
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
