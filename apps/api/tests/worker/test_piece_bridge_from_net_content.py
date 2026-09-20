"""_build_conversions data-driven bridge: a mapping to a unit/pack product with a real net content
yields a unidad->g/ml conversion (so gram/ml recipes cost against it), and the hardcoded
_PIECE_GRAMS values keep precedence for the ingredients listed there."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductVariant,
    Retailer,
)
from cestaplan_api.services.planning_context import _PIECE_GRAMS, _build_conversions


def _map_ingredient_to_variant(
    db: Session, canonical: str, net_qty: str, net_unit: str, slug: str
) -> None:
    ing = Ingredient(canonical_name=canonical, display_name=canonical, is_synthetic=True)
    retailer = Retailer(slug=slug, name=slug, adapter_key="test", is_synthetic=True)
    product = Product(name=f"{canonical} product", is_synthetic=True)
    db.add_all([ing, retailer, product])
    db.flush()
    external = ExternalProduct(retailer_id=retailer.id, external_id=f"{slug}-1")
    db.add(external)
    db.flush()
    pv = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name=f"{canonical} pack",
        package_quantity=Decimal("1"),
        package_unit="unit",
        net_content_quantity=Decimal(net_qty),
        net_content_unit=net_unit,
    )
    db.add(pv)
    db.flush()
    db.add(IngredientProductMapping(
        ingredient_id=ing.id, product_id=product.id, product_variant_id=pv.id,
        retailer_id=retailer.id, is_active=True,
    ))
    db.flush()


def _factor(convs, canonical: str, to_unit: str) -> Decimal | None:
    for c in convs:
        if c.canonical_name == canonical and c.from_unit == "unidad" and c.to_unit == to_unit:
            return c.factor
    return None


def test_net_content_yields_unidad_bridge(db_session: Session) -> None:
    _map_ingredient_to_variant(db_session, "brik_test_unico", "0.5", "l", "brk-ret")
    convs = _build_conversions(db_session)
    # 0.5 l -> 500 ml bridge for a product billed per "unit".
    assert _factor(convs, "brik_test_unico", "ml") == Decimal("500")


def test_hardcoded_piece_grams_takes_precedence(db_session: Session) -> None:
    # coliflor is in _PIECE_GRAMS (600 g/pieza). Even with a net-content mapping declaring a
    # different weight, the conventional value must win (data-driven skips hardcoded names).
    assert "coliflor" in _PIECE_GRAMS
    _map_ingredient_to_variant(db_session, "coliflor", "0.9", "kg", "col-ret")
    convs = _build_conversions(db_session)
    factors = [
        c.factor for c in convs
        if c.canonical_name == "coliflor" and c.from_unit == "unidad" and c.to_unit == "g"
    ]
    assert factors == [Decimal(str(_PIECE_GRAMS["coliflor"]))]  # 600, not 900
