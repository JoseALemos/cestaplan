"""Normalization of parsed products and prices (pure, no DB, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cestaplan_api.ingestion.contracts import PromotionType
from cestaplan_api.ingestion.normalization import (
    NormalizationError,
    ParsedProduct,
    PriceNormalizer,
    ProductNormalizer,
    PromotionParser,
    canonical_unit,
    to_decimal,
)

# --------------------------------------------------------------------------- #
# Product normalization
# --------------------------------------------------------------------------- #


def test_product_name_cleanup_and_canonical_units() -> None:
    norm = ProductNormalizer()
    out = norm.normalize(
        ParsedProduct(
            name="  Pechuga   de  pollo  ",
            brand="  Hacendado ",
            package_quantity="500",
            package_unit="gramos",
        )
    )
    assert out.name == "Pechuga de pollo"
    assert out.brand == "Hacendado"
    assert out.package_unit == "g"
    assert out.package_quantity == Decimal("500")
    assert out.package_count == 1
    # 500 g -> 0.5 kg base quantity.
    assert out.base_unit == "kg"
    assert out.base_quantity == Decimal("0.5")


def test_product_multipack_recovered_from_name() -> None:
    norm = ProductNormalizer()
    out = norm.normalize(ParsedProduct(name="Refresco cola 6x330ml"))
    assert out.package_count == 6
    assert out.package_quantity == Decimal("330")
    assert out.package_unit == "ml"
    assert out.base_unit == "l"
    # 6 * 330 ml = 1980 ml = 1.98 l
    assert out.base_quantity == Decimal("1.980")


def test_product_unknown_unit_rejected() -> None:
    norm = ProductNormalizer()
    with pytest.raises(NormalizationError):
        norm.normalize(ParsedProduct(name="x", package_quantity="1", package_unit="furlongs"))


def test_canonical_unit_synonyms() -> None:
    assert canonical_unit("Kg") == "kg"
    assert canonical_unit("litros") == "l"
    assert canonical_unit("uds") == "unit"
    assert canonical_unit("mystery") is None
    assert canonical_unit(None) is None


# --------------------------------------------------------------------------- #
# Price normalization: money is Decimal, missing -> None, €/kg coherent
# --------------------------------------------------------------------------- #


def test_price_amount_is_decimal_and_quantized() -> None:
    pn = PriceNormalizer()
    out = pn.normalize("3.49", "EUR", package_quantity="500", package_unit="g")
    assert isinstance(out.amount, Decimal)
    assert out.amount == Decimal("3.49")
    assert out.currency == "EUR"


def test_price_unit_amount_per_kg_computed_right() -> None:
    pn = PriceNormalizer()
    out = pn.normalize("3.49", "EUR", package_quantity="500", package_unit="g")
    # 3.49 / 0.5 kg = 6.98 €/kg
    assert out.unit_code == "kg"
    assert out.unit_amount == Decimal("6.98")


def test_price_unit_amount_per_litre_with_multipack() -> None:
    pn = PriceNormalizer()
    out = pn.normalize(
        "5.94", "EUR", package_quantity="330", package_unit="ml", package_count=6
    )
    # 5.94 / 1.98 l = 3.00 €/l
    assert out.unit_code == "l"
    assert out.unit_amount == Decimal("3")


def test_price_unit_amount_per_unit() -> None:
    pn = PriceNormalizer()
    out = pn.normalize("2.40", "EUR", package_quantity="6", package_unit="unit")
    assert out.unit_code == "unit"
    assert out.unit_amount == Decimal("0.4")


def test_missing_amount_yields_none_not_zero() -> None:
    pn = PriceNormalizer()
    out = pn.normalize(None, "EUR", package_quantity="500", package_unit="g")
    assert out.amount is None
    assert out.unit_amount is None  # missing, never 0


def test_missing_package_yields_none_unit_amount() -> None:
    pn = PriceNormalizer()
    out = pn.normalize("3.49", "EUR")
    assert out.amount == Decimal("3.49")
    assert out.unit_amount is None


def test_unknown_currency_rejected() -> None:
    pn = PriceNormalizer()
    with pytest.raises(NormalizationError):
        pn.normalize("3.49", "XYZ", package_quantity="1", package_unit="kg")


def test_currency_defaults_to_eur_and_uppercases() -> None:
    pn = PriceNormalizer()
    out = pn.normalize("1.00", "eur", package_quantity="1", package_unit="kg")
    assert out.currency == "EUR"


def test_to_decimal_never_uses_float() -> None:
    # A string with a comma decimal separator is parsed exactly.
    assert to_decimal("3,49") == Decimal("3.49")
    assert to_decimal(None) is None
    assert to_decimal("") is None
    assert to_decimal(5) == Decimal("5")


# --------------------------------------------------------------------------- #
# Promotion parsing: modelled rules, raw_text kept, not collapsed to a price
# --------------------------------------------------------------------------- #


def test_promo_2x1_nxm() -> None:
    p = PromotionParser().parse("2x1 en toda la gama")
    assert p is not None
    assert p.promotion_type is PromotionType.NXM
    assert p.required_quantity == 2
    assert p.charged_quantity == 1
    assert p.raw_text == "2x1 en toda la gama"


def test_promo_3x2_nxm() -> None:
    p = PromotionParser().parse("Oferta 3x2")
    assert p is not None
    assert p.promotion_type is PromotionType.NXM
    assert p.required_quantity == 3
    assert p.charged_quantity == 2


def test_promo_second_unit_percentage() -> None:
    p = PromotionParser().parse("Segunda unidad al 50%")
    assert p is not None
    assert p.promotion_type is PromotionType.SECOND_UNIT
    assert p.required_quantity == 2
    assert p.percentage_discount == Decimal("50")


def test_promo_second_unit_free() -> None:
    p = PromotionParser().parse("segunda unidad gratis")
    assert p is not None
    assert p.promotion_type is PromotionType.SECOND_UNIT
    assert p.percentage_discount == Decimal("100")


def test_promo_percentage_off() -> None:
    p = PromotionParser().parse("-20% de descuento")
    assert p is not None
    assert p.promotion_type is PromotionType.PERCENTAGE
    assert p.percentage_discount == Decimal("20")


def test_promo_fixed_discount() -> None:
    p = PromotionParser().parse("Ahorra 1,50€")
    assert p is not None
    assert p.promotion_type is PromotionType.FIXED
    assert p.fixed_discount == Decimal("1.50")


def test_promo_min_quantity() -> None:
    p = PromotionParser().parse("Comprando 3 un 25% de descuento")
    assert p is not None
    assert p.promotion_type is PromotionType.MIN_QUANTITY
    assert p.required_quantity == 3
    assert p.percentage_discount == Decimal("25")


def test_promo_pack() -> None:
    p = PromotionParser().parse("Pack de 6")
    assert p is not None
    assert p.promotion_type is PromotionType.PACK
    assert p.required_quantity == 6


def test_promo_loyalty_flag_and_dates() -> None:
    p = PromotionParser().parse(
        "Segunda unidad al 50% solo con tarjeta del 01/07 al 15/07",
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert p is not None
    assert p.loyalty_required is True
    assert p.valid_from == datetime(2026, 7, 1, tzinfo=UTC)
    assert p.valid_until == datetime(2026, 7, 15, tzinfo=UTC)


def test_promo_not_collapsed_to_single_price() -> None:
    # A 2x1 keeps its rule shape; it is never reduced to one effective unit price.
    p = PromotionParser().parse("2x1")
    assert p is not None
    assert p.required_quantity == 2 and p.charged_quantity == 1
    # No "effective price" field exists on the promotion model.
    assert not hasattr(p, "effective_unit_price")


def test_promo_empty_and_none() -> None:
    parser = PromotionParser()
    assert parser.parse(None) is None
    assert parser.parse("   ") is None
    assert parser.parse("sin oferta relevante") is None


def test_multipack_not_confused_with_nxm() -> None:
    # "6x330ml" is a package shape, not a 6-for-330 promotion.
    assert PromotionParser().parse("Refresco 6x330ml") is None
