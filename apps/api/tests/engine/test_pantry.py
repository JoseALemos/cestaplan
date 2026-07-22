"""Pantry accounting (OPTIMIZATION.md §2.5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_engine.contracts import PantryItemDTO
from cestaplan_engine.pantry import PantryCalculator
from cestaplan_engine.units import UnitConverter


def test_pantry_reduces_pending():
    pantry = [PantryItemDTO(canonical_name="rice", quantity=Decimal("200"), unit="g")]
    calc = PantryCalculator(pantry, UnitConverter())
    used, pending = calc.pending("rice", Decimal("600"), "g")
    assert used == Decimal("200")
    assert pending == Decimal("400")


def test_pantry_covers_all():
    pantry = [PantryItemDTO(canonical_name="rice", quantity=Decimal("800"), unit="g")]
    calc = PantryCalculator(pantry, UnitConverter())
    used, pending = calc.pending("rice", Decimal("600"), "g")
    assert used == Decimal("600")
    assert pending == Decimal("0")


def test_expired_pantry_ignored():
    pantry = [
        PantryItemDTO(
            canonical_name="rice",
            quantity=Decimal("500"),
            unit="g",
            expires_at=date(2026, 7, 1),
        )
    ]
    calc = PantryCalculator(pantry, UnitConverter(), as_of=date(2026, 7, 21))
    used, pending = calc.pending("rice", Decimal("600"), "g")
    assert used == Decimal("0")
    assert pending == Decimal("600")


def test_pantry_unit_conversion():
    pantry = [PantryItemDTO(canonical_name="rice", quantity=Decimal("1"), unit="kg")]
    calc = PantryCalculator(pantry, UnitConverter())
    used, pending = calc.pending("rice", Decimal("600"), "g")
    assert used == Decimal("600")
    assert pending == Decimal("0")
