"""A per-piece weight (unidad->g) must let the engine price a gram recipe against a product sold
by "unit" — the bridge g -> unidad -> unit (count units are interchangeable). Same for ml via a
per-package volume. Regression guard for the "cannot convert 'g' -> 'unit'; line left without
price" warnings on ingredients whose Mercadona product is billed per unit/pack.
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine.contracts import IngredientConversionDTO
from cestaplan_engine.units import UnitConverter


def _conv(canonical: str, to_unit: str, factor: str) -> IngredientConversionDTO:
    return IngredientConversionDTO(
        canonical_name=canonical, from_unit="unidad", to_unit=to_unit, factor=Decimal(factor)
    )


def test_grams_recipe_prices_against_a_unit_product() -> None:
    # coliflor: 1 unit = 600 g. A 300 g recipe line must resolve to 0.5 "unit" (not fail).
    conv = UnitConverter([_conv("coliflor", "g", "600")])
    # 300 / 600 = 0.5 (a Decimal division tail is fine; the packager quantizes when buying packs).
    got = conv.convert(Decimal("300"), "g", "unit", "coliflor")
    assert abs(got - Decimal("0.5")) < Decimal("0.0001")
    # And the reverse: one whole unit is 600 g.
    assert conv.convert(Decimal("1"), "unit", "g", "coliflor") == Decimal("600")


def test_ml_recipe_prices_against_a_unit_product() -> None:
    # nata: 1 unit (brik) = 600 ml. A 150 ml recipe line resolves to 0.25 "unit".
    conv = UnitConverter([_conv("nata", "ml", "600")])
    got = conv.convert(Decimal("150"), "ml", "unit", "nata")
    assert abs(got - Decimal("0.25")) < Decimal("0.0001")


def test_no_piece_weight_still_fails_closed() -> None:
    # Without a registered piece weight the cross-dimension conversion is refused (never guessed).
    from cestaplan_engine.units import ConversionError

    conv = UnitConverter([])
    try:
        conv.convert(Decimal("300"), "g", "unit", "sardina")
    except ConversionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ConversionError without a piece weight")
