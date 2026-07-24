"""Per-recipe shadow comparison (spec §11) — DB-backed, no network.

Money is produced ONLY when the same recipe is fully costable on BOTH the provider (staging) side
and the baseline side. Every other case leaves the monetary fields NULL and records a blocker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    IngredientProductMapping,
    PriceObservation,
    Product,
    ProductPrice,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
    Store,
)
from cestaplan_api.services.recipe_shadow import RecipeShadowStatus, compare_recipe_shadow

_NOW = datetime.now(UTC)
_PROV = "test-shadow-prov"
_BASE = "test-shadow-base"
_AVENA, _LECHE, _PLATANO = "avena_copos", "leche_entera", "platano"


def _ing(db: Session, name: str) -> int:
    """Resolve a seeded ingredient's id by canonical name (CI-safe; seed ids are serial)."""
    return db.execute(select(Ingredient.id).where(Ingredient.canonical_name == name)).scalar_one()


def _provider_product(
    db: Session, rid: int, ing_id: str, key: str, name: str, qty: str, unit: str, price: str
) -> None:
    product = Product(name=name, is_synthetic=False)
    db.add(product)
    db.flush()
    ext = ExternalProduct(retailer_id=rid, external_id=f"EXT-{name[:18]}")
    db.add(ext)
    db.flush()
    variant = ProductVariant(
        retailer_id=rid,
        external_product_id=ext.id,
        product_id=product.id,
        display_name=name,
        sell_unit="package",
        net_content_quantity=Decimal(qty),
        net_content_unit=unit,
    )
    db.add(variant)
    db.flush()
    db.add(
        PriceObservation(
            retailer_id=rid,
            product_variant_id=variant.id,
            price_scope="national",
            price_type="regular",
            amount=Decimal(price),
            currency="EUR",
            observed_at=_NOW,
            imported_at=_NOW,
            valid_from=_NOW,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    db.add(
        ProviderIngredientMapping(
            provider_code=_PROV,
            ingredient_id=_ing(db, ing_id),
            canonical_ingredient_key=key,
            retailer_slug=_PROV,
            external_product_id=ext.external_id,
            normalized_product_id=product.id,
            mapping_status="auto_approved",
            mapping_method="exact_alias",
            confidence_score=Decimal("0.96"),
            unit_compatibility="compatible",
            required_review=False,
            active=True,
        )
    )
    db.flush()


def _baseline_product(
    db: Session, rid: int, store_id: int, ing_id: str, name: str, qty: str, unit: str, price: str
) -> None:
    product = Product(
        name=name,
        is_synthetic=True,
        package_quantity=Decimal(qty),
        package_unit=unit,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductPrice(
            retailer_id=rid,
            store_id=store_id,
            product_id=product.id,
            amount=Decimal(price),
            currency="EUR",
            package_quantity=Decimal(qty),
            package_unit=unit,
            source_type="demo",
            source_name="demo",
            observed_at=_NOW,
            imported_at=_NOW,
            confidence_score=Decimal("1.0"),
        )
    )
    db.add(
        IngredientProductMapping(
            retailer_id=rid, ingredient_id=_ing(db, ing_id), product_id=product.id, is_active=True
        )
    )
    db.flush()


def _recipe(db: Session) -> Recipe:
    recipe = Recipe(origin="seed", title="Shadow Recipe", servings=2, is_synthetic=True)
    db.add(recipe)
    db.flush()
    for ing_id, key, qty, unit in [
        (_AVENA, "avena_copos", "80", "g"),
        (_LECHE, "leche_entera", "400", "ml"),
        (_PLATANO, "platano", "160", "g"),
    ]:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=_ing(db, ing_id),
                canonical_name=key,
                display_name=key,
                quantity=Decimal(qty),
                unit=unit,
                optional=False,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def _provider_retailer(db: Session) -> int:
    r = Retailer(slug=_PROV, name="Shadow Prov", adapter_key="test", is_synthetic=True)
    db.add(r)
    db.flush()
    _provider_product(db, r.id, _AVENA, "avena_copos", "Copos 500g", "500", "g", "0.75")
    _provider_product(db, r.id, _LECHE, "leche_entera", "Leche 1L", "1000", "ml", "0.95")
    _provider_product(db, r.id, _PLATANO, "platano", "Plátano 700g", "700", "g", "2.49")
    return r.id


def _baseline_retailer(db: Session) -> int:
    r = Retailer(slug=_BASE, name="Shadow Base", adapter_key="demo", is_synthetic=True)
    db.add(r)
    db.flush()
    store = Store(retailer_id=r.id, name="Base Store", is_synthetic=True)
    db.add(store)
    db.flush()
    _baseline_product(db, r.id, store.id, _AVENA, "Copos demo 500g", "500", "g", "1.06")
    _baseline_product(db, r.id, store.id, _LECHE, "Leche demo 1L", "1000", "ml", "0.76")
    _baseline_product(db, r.id, store.id, _PLATANO, "Plátano demo 1kg", "1", "kg", "1.32")
    return r.id


def test_comparable_when_both_sides_fully_costable(db_session: Session) -> None:
    _provider_retailer(db_session)
    _baseline_retailer(db_session)
    recipe = _recipe(db_session)

    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)

    assert cmp.comparison_status == RecipeShadowStatus.COMPARABLE.value
    assert cmp.provider.fully_costable and cmp.baseline.fully_costable
    assert cmp.provider_cost == Decimal("4.19")  # 0.75 + 0.95 + 2.49
    assert cmp.baseline_cost == Decimal("3.14")  # 1.06 + 0.76 + 1.32
    assert cmp.absolute_difference == Decimal("1.05")  # provider dearer
    assert cmp.percentage_difference is not None and cmp.percentage_difference > 0
    assert not cmp.blockers


def test_missing_baseline_leaves_money_null(db_session: Session) -> None:
    _provider_retailer(db_session)
    recipe = _recipe(db_session)  # no baseline retailer/mappings at all

    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)

    assert cmp.comparison_status != RecipeShadowStatus.COMPARABLE.value
    assert cmp.absolute_difference is None  # never a fabricated diff
    assert cmp.percentage_difference is None
    assert cmp.provider_cost is None
    assert cmp.blockers


def test_provider_not_costable_leaves_money_null(db_session: Session) -> None:
    # Baseline fully costable, but the provider has no usable data -> not comparable, money null.
    _baseline_retailer(db_session)
    r = Retailer(slug=_PROV, name="Empty Prov", adapter_key="test", is_synthetic=True)
    db_session.add(r)
    db_session.flush()
    recipe = _recipe(db_session)

    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)

    assert cmp.comparison_status == RecipeShadowStatus.PROVIDER_NOT_COSTABLE.value
    assert cmp.provider.fully_costable is False
    assert cmp.absolute_difference is None
    assert cmp.percentage_difference is None
