"""Quantity-aware recipe costing engine (spec §9) — DB-backed, no network.

Every scenario is built inside the test transaction on a throwaway provider/retailer, so it never
depends on (or pollutes) real seed data. Invariants under test: whole-package purchasing, cheapest
selection, surplus accounting, optional ingredients, and the hard guards (zero price, incompatible
units, pending candidates, unresolved packages, production-only prices).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
)
from cestaplan_api.services.recipe_costing import cost_recipe

_NOW = datetime.now(UTC)
_PROV = "test-costing-prov"

# Seed ingredient ids (present in the dev/test DB seed).
_AVENA, _LECHE, _PLATANO, _MIEL = "avena_copos", "leche_entera", "platano", "miel"


def _ing(db: Session, name: str) -> int:
    """Resolve a seeded ingredient's id by canonical name (CI-safe; seed ids are serial)."""
    return db.execute(select(Ingredient.id).where(Ingredient.canonical_name == name)).scalar_one()


def _retailer(db: Session) -> int:
    r = Retailer(slug=_PROV, name="Test Costing Retailer", adapter_key="test", is_synthetic=True)
    db.add(r)
    db.flush()
    return r.id


def _add_product(
    db: Session,
    retailer_id: int,
    ingredient_id: str,
    key: str,
    *,
    name: str,
    net_qty: str | None,
    net_unit: str | None,
    price: str | None,
    active_mapping: bool = True,
    mapping_status: str = "auto_approved",
    variable_weight: bool = False,
    staging: bool = True,
    ext: str | None = None,
) -> int:
    ext = ext or f"EXT-{name[:20]}"
    product = Product(name=name, is_synthetic=False)
    db.add(product)
    db.flush()
    external = ExternalProduct(retailer_id=retailer_id, external_id=ext)
    db.add(external)
    db.flush()
    variant = ProductVariant(
        retailer_id=retailer_id,
        external_product_id=external.id,
        product_id=product.id,
        display_name=name,
        sell_unit="package",
        variable_weight=variable_weight,
        net_content_quantity=Decimal(net_qty) if net_qty is not None else None,
        net_content_unit=net_unit,
    )
    db.add(variant)
    db.flush()
    if price is not None:
        db.add(
            PriceObservation(
                retailer_id=retailer_id,
                product_variant_id=variant.id,
                price_scope="national",
                price_type="regular",
                amount=Decimal(price),
                currency="EUR",
                observed_at=_NOW,
                imported_at=_NOW,
                valid_from=_NOW,
                confidence_score=Decimal("1.0"),
                staging_only=staging,
            )
        )
    db.add(
        ProviderIngredientMapping(
            provider_code=_PROV,
            ingredient_id=_ing(db, ingredient_id),
            canonical_ingredient_key=key,
            retailer_slug=_PROV,
            external_product_id=ext,
            normalized_product_id=product.id,
            mapping_status=mapping_status,
            mapping_method="exact_alias",
            confidence_score=Decimal("0.96"),
            unit_compatibility="compatible",
            required_review=not active_mapping,
            active=active_mapping,
        )
    )
    db.flush()
    return product.id


def _recipe(
    db: Session, ings: list[tuple[str, str, str, str, str, bool]], *, servings: int = 2
) -> Recipe:
    recipe = Recipe(origin="seed", title="Test Recipe", servings=servings, is_synthetic=True)
    db.add(recipe)
    db.flush()
    for ing_id, key, display, qty, unit, optional in ings:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=_ing(db, ing_id),
                canonical_name=key,
                display_name=display,
                quantity=Decimal(qty),
                unit=unit,
                optional=optional,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def test_fully_costable_picks_cheapest_and_accounts_surplus(db_session: Session) -> None:
    rid = _retailer(db_session)
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos 500g",
        net_qty="500",
        net_unit="g",
        price="0.75",
    )
    # Two milk packs: the 1 L @0.95 is cheaper for 400 ml than the 6 L @5.70.
    _add_product(
        db_session,
        rid,
        _LECHE,
        "leche_entera",
        name="Leche 6x1L",
        net_qty="6000",
        net_unit="ml",
        price="5.70",
        ext="EXT-LECHE-6",
    )
    _add_product(
        db_session,
        rid,
        _LECHE,
        "leche_entera",
        name="Leche 1L",
        net_qty="1000",
        net_unit="ml",
        price="0.95",
        ext="EXT-LECHE-1",
    )
    _add_product(
        db_session,
        rid,
        _PLATANO,
        "platano",
        name="Plátano 700g",
        net_qty="700",
        net_unit="g",
        price="2.49",
    )
    recipe = _recipe(
        db_session,
        [
            (_AVENA, "avena_copos", "Copos de avena", "80", "g", False),
            (_LECHE, "leche_entera", "Leche entera", "400", "ml", False),
            (_PLATANO, "platano", "Plátano", "160", "g", False),
        ],
    )

    result = cost_recipe(db_session, recipe, _PROV)

    assert result.fully_costable is True
    assert result.total_purchase_cost == Decimal("4.19")  # 0.75 + 0.95 + 2.49
    leche = next(line for line in result.lines if line.canonical_name == "leche_entera")
    assert leche.package_price == Decimal("0.95")  # cheapest pack chosen, not the 6 L one
    assert leche.units_purchased == Decimal("1")
    assert leche.surplus_quantity == Decimal("600.0000")
    assert leche.surplus_value == Decimal("0.57")  # 0.95 - consumed(0.38)
    # Per serving over 2 servings.
    assert result.cost_per_serving_purchase == Decimal("2.10")  # 4.19 / 2 -> 2.095 -> 2.10


def test_optional_uncostable_ingredient_does_not_block(db_session: Session) -> None:
    rid = _retailer(db_session)
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos 500g",
        net_qty="500",
        net_unit="g",
        price="0.75",
    )
    recipe = _recipe(
        db_session,
        [
            (_AVENA, "avena_copos", "Copos", "80", "g", False),
            (_MIEL, "miel", "Miel", "15", "g", True),  # optional, unmapped
        ],
    )
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is True  # optional miel absence never blocks
    miel = next(line for line in result.lines if line.canonical_name == "miel")
    assert miel.costable is False


def test_zero_price_makes_mandatory_ingredient_uncostable(db_session: Session) -> None:
    rid = _retailer(db_session)
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos 0€",
        net_qty="500",
        net_unit="g",
        price="0",
    )
    recipe = _recipe(db_session, [(_AVENA, "avena_copos", "Copos", "80", "g", False)])
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is False
    assert result.total_purchase_cost is None  # never invents a cost


def test_incompatible_units_are_not_costable(db_session: Session) -> None:
    rid = _retailer(db_session)
    # Recipe needs grams (mass) but the only product is a volume pack.
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos ml",
        net_qty="500",
        net_unit="ml",
        price="0.75",
    )
    recipe = _recipe(db_session, [(_AVENA, "avena_copos", "Copos", "80", "g", False)])
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is False


def test_pending_candidate_is_never_used(db_session: Session) -> None:
    rid = _retailer(db_session)
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos pending",
        net_qty="500",
        net_unit="g",
        price="0.75",
        active_mapping=False,
        mapping_status="candidate",
    )
    recipe = _recipe(db_session, [(_AVENA, "avena_copos", "Copos", "80", "g", False)])
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is False  # a candidate awaiting review is not selectable


def test_unresolved_package_is_not_costable(db_session: Session) -> None:
    rid = _retailer(db_session)
    # No net content and not variable-weight -> UNRESOLVED costing mode.
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos suelto",
        net_qty=None,
        net_unit=None,
        price="0.75",
    )
    recipe = _recipe(db_session, [(_AVENA, "avena_copos", "Copos", "80", "g", False)])
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is False


def test_production_only_price_is_not_used(db_session: Session) -> None:
    rid = _retailer(db_session)
    # A production (non-staging) price must NOT be visible to the staging costing engine.
    _add_product(
        db_session,
        rid,
        _AVENA,
        "avena_copos",
        name="Copos prod",
        net_qty="500",
        net_unit="g",
        price="0.75",
        staging=False,
    )
    recipe = _recipe(db_session, [(_AVENA, "avena_copos", "Copos", "80", "g", False)])
    result = cost_recipe(db_session, recipe, _PROV)
    assert result.fully_costable is False  # staging engine never reads production prices
