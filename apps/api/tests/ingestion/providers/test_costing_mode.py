"""Per-product costing-mode classification (audit) — offline, no network.

A bare ``unit_price`` is never enough: a fixed package that only shows a reference €/kg is
UNRESOLVED. Only a known net content (fixed package), a genuine sale by weight/volume, or a
known unit count makes a product costable. Coverage is aggregated AFTER classifying each product.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    PriceScope,
    ProductCostingMode,
    SellUnit,
)
from cestaplan_api.ingestion.providers.onboarding import classify_costing_mode, measure_coverage

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _product(**over: Any) -> ExternalCatalogProduct:
    base: dict[str, Any] = {
        "provider": "p",
        "retailer_slug": "r",
        "external_product_id": "x1",
        "product_name": "Producto",
        "sell_unit": SellUnit.PACKAGE,
        "regular_price": Decimal("1.99"),
        "currency": "EUR",
        "price_scope": PriceScope.POSTAL_CODE,
        "observed_at": _NOW,
        "availability": Availability.IN_STOCK,
    }
    base.update(over)
    return ExternalCatalogProduct(**base)


def test_fixed_tray_with_reference_kg_price_is_unresolved() -> None:
    # A fixed tray that only shows an informational €/kg — no net content, not sold loose.
    tray = _product(
        product_name="Tomate cherry pera 250 g",
        sell_unit=SellUnit.PACKAGE,
        variable_weight=False,
        unit_price=Decimal("4.00"),
        unit_price_unit="kg",
        net_content_quantity=None,
        net_content_unit=None,
    )
    assert classify_costing_mode(tray) is ProductCostingMode.UNRESOLVED


def test_meat_sold_by_weight_is_variable_weight() -> None:
    meat = _product(
        product_name="Filete de ternera al peso",
        sell_unit=SellUnit.WEIGHT,
        variable_weight=True,
        unit_price=Decimal("12.90"),
        unit_price_unit="kg",
    )
    assert classify_costing_mode(meat) is ProductCostingMode.VARIABLE_WEIGHT


def test_juice_sold_by_volume_is_variable_volume() -> None:
    juice = _product(
        product_name="Zumo natural a granel",
        sell_unit=SellUnit.VOLUME,
        variable_weight=True,
        unit_price=Decimal("2.20"),
        unit_price_unit="l",
    )
    assert classify_costing_mode(juice) is ProductCostingMode.VARIABLE_VOLUME


def test_unit_price_without_variable_evidence_is_unresolved() -> None:
    # unit_price present but the item is a fixed package (no variable-sale evidence) -> unresolved.
    p = _product(
        sell_unit=SellUnit.PACKAGE,
        variable_weight=False,
        unit_price=Decimal("1.96"),
        unit_price_unit="kg",
    )
    assert classify_costing_mode(p) is ProductCostingMode.UNRESOLVED


def test_single_piece_is_discrete_unit() -> None:
    piece = _product(
        product_name="Baguette 1 ud",
        sell_unit=SellUnit.UNIT,
        variable_weight=False,
        package_quantity=Decimal("1"),
    )
    assert classify_costing_mode(piece) is ProductCostingMode.DISCRETE_UNIT


def test_multipack_of_units_is_discrete_unit() -> None:
    multipack = _product(
        product_name="Pack 6 unidades",
        sell_unit=SellUnit.UNIT,
        variable_weight=False,
        package_quantity=Decimal("6"),
    )
    assert classify_costing_mode(multipack) is ProductCostingMode.DISCRETE_UNIT


def test_fixed_package_costs_a_sub_package_recipe_amount() -> None:
    # A recipe needing 100 g is costable pro-rata from a 500 g package with known net content.
    pack = _product(
        product_name="Harina 500 g",
        sell_unit=SellUnit.PACKAGE,
        net_content_quantity=Decimal("500"),
        net_content_unit=ContentUnit.G,
    )
    assert classify_costing_mode(pack) is ProductCostingMode.FIXED_PACKAGE


def test_variable_weight_supports_min_weight_increment_rules() -> None:
    # The variable_weight mode is the one that carries min-weight / increment rules downstream;
    # here it must at least be classified as costable variable weight (never unresolved).
    loose = _product(
        product_name="Manzana a granel",
        sell_unit=SellUnit.WEIGHT,
        variable_weight=True,
        unit_price=Decimal("1.99"),
        unit_price_unit="kg",
    )
    assert classify_costing_mode(loose) is ProductCostingMode.VARIABLE_WEIGHT


def test_ambiguous_product_is_rejected_for_costing() -> None:
    # No net content, no unit price, no unit count -> cannot cost a recipe.
    ambiguous = _product(sell_unit=SellUnit.PACKAGE, variable_weight=False)
    assert classify_costing_mode(ambiguous) is ProductCostingMode.UNRESOLVED


def test_aggregate_coverage_reflects_per_product_modes() -> None:
    products = [
        _product(net_content_quantity=Decimal("500"), net_content_unit=ContentUnit.G),  # fixed
        _product(  # variable weight
            sell_unit=SellUnit.WEIGHT,
            variable_weight=True,
            unit_price=Decimal("9.9"),
            unit_price_unit="kg",
        ),
        _product(  # unresolved: reference €/kg only
            sell_unit=SellUnit.PACKAGE,
            variable_weight=False,
            unit_price=Decimal("4.0"),
            unit_price_unit="kg",
        ),
        _product(sell_unit=SellUnit.PACKAGE, variable_weight=False),  # unresolved
    ]
    cov = measure_coverage(
        products, captured=4, limit=10, supports_full_catalog=False, supports_store_scope=True
    )
    assert cov.package_coverage == Decimal("0.2500")
    assert cov.variable_weight_coverage == Decimal("0.2500")
    assert cov.unresolved_costing_coverage == Decimal("0.5000")
    assert cov.costing_eligible_product_coverage == Decimal("0.5000")
    assert cov.costing_eligibility == "insufficient"  # 0.50 < 0.80 threshold
