"""Shopping-list price/cost semantics (audit) — pure, no DB, no network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.services.shopping_semantics import (
    PriceSourceKind,
    line_cost_breakdown,
    normalized_unit_price,
    package_price,
    resolve_source_kind,
)


def test_package_price_is_the_whole_package_price() -> None:
    # Aceite 500 ml a 3,19 € (un envase): 3,19 €/envase — NEVER a per-gram value.
    assert package_price(Decimal("3.19"), 1) == Decimal("3.19")
    # Two jars for 1,62 € -> one jar is 0,81 €.
    assert package_price(Decimal("1.62"), 2) == Decimal("0.81")
    assert package_price(None, 1) is None
    assert package_price(Decimal("1.00"), 0) is None
    assert package_price(Decimal("1.00"), None) is None


def test_normalized_unit_price_per_kg_l_unit() -> None:
    # 3,19 € / 500 ml -> 6,38 €/l (never 0,01).
    assert normalized_unit_price(Decimal("3.19"), Decimal("500"), "ml") == (Decimal("6.38"), "l")
    # 2,97 € / 500 g -> 5,94 €/kg.
    assert normalized_unit_price(Decimal("2.97"), Decimal("500"), "g") == (Decimal("5.94"), "kg")
    # 2,08 € / 12 unidades -> 0,17 €/unidad.
    assert normalized_unit_price(Decimal("2.08"), Decimal("12"), "unit") == (
        Decimal("0.17"),
        "unidad",
    )
    # kg pack: 1,32 € / 1 kg -> 1,32 €/kg.
    assert normalized_unit_price(Decimal("1.32"), Decimal("1"), "kg") == (Decimal("1.32"), "kg")


def test_normalized_unit_price_never_rounds_a_per_gram_value_to_a_headline() -> None:
    result = normalized_unit_price(Decimal("3.19"), Decimal("500"), "ml")
    assert result is not None
    price, unit = result
    assert (price, unit) == (Decimal("6.38"), "l")
    assert price != Decimal("0.01") and price != Decimal("0.00")


def test_normalized_unit_price_none_on_missing_data() -> None:
    assert normalized_unit_price(None, Decimal("500"), "ml") is None
    assert normalized_unit_price(Decimal("3.19"), None, "ml") is None
    assert normalized_unit_price(Decimal("3.19"), Decimal("0"), "ml") is None
    assert normalized_unit_price(Decimal("3.19"), Decimal("500"), "cucharada") is None


def test_line_cost_breakdown_purchased_consumed_leftover() -> None:
    # Garbanzos: 2 jars, purchased 800 g, used 600 g, outlay 1,62 €.
    b = line_cost_breakdown(Decimal("1.62"), Decimal("800"), Decimal("600"))
    assert b["purchased_cost"] == Decimal("1.62")
    assert b["consumed_cost"] == Decimal("1.22")  # 1.62 * 600/800 = 1.215 -> 1.22
    assert b["leftover_value"] == Decimal("0.40")  # 1.62 - 1.22


def test_line_cost_breakdown_full_consumption_has_no_leftover() -> None:
    b = line_cost_breakdown(Decimal("0.94"), Decimal("500"), Decimal("500"))
    assert b["purchased_cost"] == Decimal("0.94")
    assert b["consumed_cost"] == Decimal("0.94")
    assert b["leftover_value"] == Decimal("0.00")


def test_unknown_cost_is_never_zero() -> None:
    b = line_cost_breakdown(None, Decimal("500"), Decimal("100"))
    assert b == {"purchased_cost": None, "consumed_cost": None, "leftover_value": None}


@pytest.mark.parametrize(
    ("source_type", "price_status", "expected"),
    [
        ("demo", "known", PriceSourceKind.DEMO),  # demo stays demo even when 'known'
        ("open_dataset", "known", PriceSourceKind.CONFIRMED_EXTERNAL),
        ("official", "known", PriceSourceKind.CONFIRMED_EXTERNAL),
        ("estimated", "estimated", PriceSourceKind.ESTIMATED),
        ("official", "estimated", PriceSourceKind.ESTIMATED),
        (None, "missing", PriceSourceKind.UNAVAILABLE),
        (None, "unknown", PriceSourceKind.UNAVAILABLE),
    ],
)
def test_resolve_source_kind(source_type: str | None, price_status: str, expected) -> None:
    assert resolve_source_kind(source_type, price_status) == expected
