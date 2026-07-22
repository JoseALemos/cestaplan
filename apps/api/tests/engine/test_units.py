"""Unit conversion exactness (OPTIMIZATION.md §2.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_engine.contracts import IngredientConversionDTO
from cestaplan_engine.units import ConversionError, UnitConverter


def test_mass_conversions_exact():
    u = UnitConverter()
    assert u.convert(Decimal("1"), "kg", "g") == Decimal("1000")
    assert u.convert(Decimal("500"), "g", "kg") == Decimal("0.5")
    assert u.convert(Decimal("2.5"), "kg", "g") == Decimal("2500")


def test_volume_conversions_exact():
    u = UnitConverter()
    assert u.convert(Decimal("1"), "l", "ml") == Decimal("1000")
    assert u.convert(Decimal("250"), "ml", "l") == Decimal("0.25")


def test_same_unit_is_identity():
    u = UnitConverter()
    assert u.convert(Decimal("7"), "g", "g") == Decimal("7")


def test_result_is_decimal_not_float():
    u = UnitConverter()
    result = u.convert(Decimal("3"), "kg", "g")
    assert isinstance(result, Decimal)


def test_cross_dimension_without_density_raises():
    u = UnitConverter()
    with pytest.raises(ConversionError):
        u.convert(Decimal("100"), "ml", "g", "milk")


def test_counted_unit_cannot_convert_to_mass():
    u = UnitConverter()
    with pytest.raises(ConversionError):
        u.convert(Decimal("2"), "unit", "g", "egg")


def test_density_conversion_ml_to_g():
    # syrup density 1.25 g/ml (clean inverse for exact assertions)
    conv = IngredientConversionDTO(
        canonical_name="syrup", from_unit="ml", to_unit="g", factor=Decimal("1.25")
    )
    u = UnitConverter([conv])
    assert u.convert(Decimal("100"), "ml", "g", "syrup") == Decimal("125.00")
    # inverse is registered automatically (1/1.25 = 0.8, exact)
    assert u.convert(Decimal("125"), "g", "ml", "syrup") == Decimal("100.0")


def test_density_bridge_l_to_g_via_ml():
    conv = IngredientConversionDTO(
        canonical_name="oil", from_unit="ml", to_unit="g", factor=Decimal("0.92")
    )
    u = UnitConverter([conv])
    # 1 l -> 1000 ml -> * 0.92 -> 920 g
    assert u.convert(Decimal("1"), "l", "g", "oil") == Decimal("920.00")


def test_unit_identity_counted():
    u = UnitConverter()
    assert u.convert(Decimal("3"), "unit", "unit", "egg") == Decimal("3")
