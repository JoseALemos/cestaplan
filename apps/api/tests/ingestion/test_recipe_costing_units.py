"""C8: el carril de sombra reconoce las mismas unidades que el motor (tabla única).

recipe_costing._to_base delega en cestaplan_engine.units.to_base, así que unidades culinarias
(cucharada, cl, pizca…) dejan de salir 'incosteables' en sombra cuando el plan sí las costea.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.services.recipe_costing import _to_base


@pytest.mark.parametrize(
    ("qty", "unit", "expected"),
    [
        ("2", "cucharada", (Decimal("30"), "volume")),   # 2 x 15 ml
        ("1", "cucharadita", (Decimal("5"), "volume")),
        ("1", "cl", (Decimal("10"), "volume")),
        ("1", "vaso", (Decimal("200"), "volume")),
        ("2", "pizca", (Decimal("1.0"), "mass")),         # 2 x 0,5 g
        ("500", "mg", (Decimal("0.500"), "mass")),
        ("500", "g", (Decimal("500"), "mass")),
        ("2", "kg", (Decimal("2000"), "mass")),
        ("3", "unidad", (Decimal("3"), "count")),
        ("3", "unit", (Decimal("3"), "count")),
    ],
)
def test_to_base_known_units(qty: str, unit: str, expected: tuple[Decimal, str]) -> None:
    assert _to_base(Decimal(qty), unit) == expected


def test_to_base_unknown_unit_is_none() -> None:
    assert _to_base(Decimal("1"), "cucharon-magico") is None
