"""Evidence-based purchase-mode resolution (audit §2) — pure, no DB, no network."""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.services.purchase_evidence import resolve_purchase_evidence


def _ev(**over: object):
    base: dict[str, object] = {
        "name": "producto",
        "required_unit": "g",
        "net_content_quantity": None,
        "net_content_unit": None,
        "variable_weight": False,
        "sell_unit": "package",
        "regular_price": Decimal("1.00"),
        "unit_price": None,
        "unit_price_unit": None,
        "has_price": True,
    }
    base.update(over)
    return resolve_purchase_evidence(**base)  # type: ignore[arg-type]


def test_fixed_tray_price_is_package_price() -> None:
    # Plátano de Canarias bandeja 700 g @2.49; unitPrice 3.56 €/kg is informational.
    ev = _ev(
        name="Plátano de Canarias bandeja 700 g",
        net_content_quantity=Decimal("700"),
        net_content_unit="g",
        regular_price=Decimal("2.49"),
        unit_price=Decimal("3.56"),
        unit_price_unit="kg",
    )
    assert ev.costing_mode == "fixed_package"
    assert ev.costing_eligible is True
    assert ev.package_price == Decimal("2.49")
    assert ev.price_is_package_price is True
    assert ev.approximate_weight is False


def test_price_per_kg_when_sold_by_weight_is_variable() -> None:
    ev = _ev(
        name="Plátano al peso",
        variable_weight=True,
        sell_unit="weight",
        regular_price=Decimal("1.99"),
        unit_price=Decimal("1.99"),
        unit_price_unit="kg",
    )
    assert ev.costing_mode == "variable_weight"
    assert ev.costing_eligible is True
    assert ev.sell_basis == "weight"


def test_approximate_weight_without_rules_is_unresolved() -> None:
    # A size range ('750g - 1250g') maps to no net content; a bare reference €/kg is not buyable.
    ev = _ev(
        name="Banana al peso",
        net_content_quantity=None,
        net_content_unit=None,
        regular_price=Decimal("2.84"),
        unit_price=Decimal("2.90"),
        unit_price_unit="kg",
    )
    assert ev.costing_mode == "unresolved"
    assert ev.costing_eligible is False
    assert ev.blocker == "approximate_weight_without_rules"
    assert ev.approximate_weight is True


def test_sold_by_piece_is_discrete_unit() -> None:
    ev = _ev(name="Aguacate unidad", required_unit="unit", sell_unit="unit")
    assert ev.costing_mode == "discrete_unit"
    assert ev.costing_eligible is True


def test_informational_unit_price_on_fixed_package_stays_package() -> None:
    # A €/kg reference alongside a real net content never turns a package into variable weight.
    ev = _ev(
        net_content_quantity=Decimal("500"),
        net_content_unit="g",
        variable_weight=False,
        regular_price=Decimal("0.75"),
        unit_price=Decimal("1.50"),
        unit_price_unit="kg",
    )
    assert ev.costing_mode == "fixed_package"
    assert ev.price_is_package_price is True


def test_incompatible_unit_blocks() -> None:
    ev = _ev(net_content_quantity=Decimal("1000"), net_content_unit="ml", required_unit="g")
    assert ev.costing_mode == "unresolved"
    assert ev.blocker == "unresolved_purchase_unit"


def test_incomplete_package_data_when_nothing_is_known() -> None:
    ev = _ev(net_content_quantity=None, net_content_unit=None, unit_price=None)
    assert ev.costing_mode == "unresolved"
    assert ev.blocker == "incomplete_package_data"


def test_zero_price_is_never_costable() -> None:
    ev = _ev(net_content_quantity=Decimal("500"), net_content_unit="g", regular_price=Decimal("0"))
    assert ev.costing_eligible is False
