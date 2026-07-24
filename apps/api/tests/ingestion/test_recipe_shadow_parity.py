"""Recipe parity + optional policy for the shadow (audit §1/§3) — DB-backed, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
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
from cestaplan_api.services import recipe_shadow as rs
from cestaplan_api.services.recipe_shadow import (
    BaselineCosting,
    RecipeShadowStatus,
    compare_recipe_shadow,
)

_NOW = datetime.now(UTC)
_PROV = "test-parity-prov"
_BASE = "test-parity-base"
_AVENA, _LECHE, _PLATANO, _MIEL = "avena_copos", "leche_entera", "platano", "miel"


def _ing(db: Session, name: str) -> int:
    """Resolve a seeded ingredient's id by canonical name (CI-safe; seed ids are serial)."""
    return db.execute(select(Ingredient.id).where(Ingredient.canonical_name == name)).scalar_one()


def _provider(db: Session) -> int:
    r = Retailer(slug=_PROV, name="Parity Prov", adapter_key="test", is_synthetic=True)
    db.add(r)
    db.flush()
    for ing_id, key, name, qty, unit, price in [
        (_AVENA, "avena_copos", "Copos 500g", "500", "g", "0.75"),
        (_LECHE, "leche_entera", "Leche 1L", "1000", "ml", "0.95"),
        (_PLATANO, "platano", "Plátano 700g", "700", "g", "2.49"),
    ]:
        product = Product(name=name, is_synthetic=False)
        db.add(product)
        db.flush()
        ext = ExternalProduct(retailer_id=r.id, external_id=f"EXT-{name[:16]}")
        db.add(ext)
        db.flush()
        v = ProductVariant(
            retailer_id=r.id,
            external_product_id=ext.id,
            product_id=product.id,
            display_name=name,
            sell_unit="package",
            net_content_quantity=Decimal(qty),
            net_content_unit=unit,
        )
        db.add(v)
        db.flush()
        db.add(
            PriceObservation(
                retailer_id=r.id,
                product_variant_id=v.id,
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
    return r.id


def _baseline(db: Session, *, with_miel: bool = True) -> int:
    r = Retailer(slug=_BASE, name="Parity Base", adapter_key="demo", is_synthetic=True)
    db.add(r)
    db.flush()
    store = Store(retailer_id=r.id, name="Base Store", is_synthetic=True)
    db.add(store)
    db.flush()
    rows = [
        (_AVENA, "Copos demo 500g", "500", "g", "1.06"),
        (_LECHE, "Leche demo 1L", "1000", "ml", "0.76"),
        (_PLATANO, "Plátano demo 1kg", "1", "kg", "1.32"),
    ]
    if with_miel:
        rows.append((_MIEL, "Miel demo 250g", "250", "g", "1.91"))
    for ing_id, name, qty, unit, price in rows:
        product = Product(
            name=name, is_synthetic=True, package_quantity=Decimal(qty), package_unit=unit
        )
        db.add(product)
        db.flush()
        db.add(
            ProductPrice(
                retailer_id=r.id,
                store_id=store.id,
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
                retailer_id=r.id,
                ingredient_id=_ing(db, ing_id),
                product_id=product.id,
                is_active=True,
            )
        )
    return r.id


def _recipe(db: Session) -> Recipe:
    recipe = Recipe(origin="seed", title="Parity Recipe", servings=2, is_synthetic=True)
    db.add(recipe)
    db.flush()
    for ing_id, key, qty, unit, opt in [
        (_AVENA, "avena_copos", "80", "g", False),
        (_LECHE, "leche_entera", "400", "ml", False),
        (_PLATANO, "platano", "160", "g", False),
        (_MIEL, "miel", "15", "g", True),
    ]:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=_ing(db, ing_id),
                canonical_name=key,
                display_name=key,
                quantity=Decimal(qty),
                unit=unit,
                optional=opt,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def test_optional_omitted_on_both_sides_is_comparable(db_session: Session) -> None:
    # miel is optional and only costable on the baseline; the shadow omits it on BOTH sides.
    _provider(db_session)
    _baseline(db_session, with_miel=True)
    recipe = _recipe(db_session)

    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)

    assert cmp.comparison_status == RecipeShadowStatus.COMPARABLE.value
    assert cmp.optional_ingredients_included == []
    assert "miel" in cmp.optional_ingredients_excluded
    assert cmp.provider_input_fingerprint == cmp.baseline_input_fingerprint
    # Mandatory-only money on both sides; miel never enters either total.
    assert cmp.provider_cost == Decimal("4.19")
    assert cmp.baseline_cost == Decimal("3.14")  # 1.06 + 0.76 + 1.32 (miel excluded)


def test_purchased_and_consumed_are_reported_separately(db_session: Session) -> None:
    _provider(db_session)
    _baseline(db_session)
    recipe = _recipe(db_session)
    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)
    assert cmp.purchased_cost_difference == Decimal("1.05")  # 4.19 - 3.14
    assert cmp.consumed_cost_difference is not None
    assert cmp.consumed_cost_difference != cmp.purchased_cost_difference  # distinct concepts
    assert cmp.reusable_leftover_value == Decimal("0.00")
    assert cmp.non_reusable_leftover_value == Decimal("0.00")
    # The provider-side leftover is wholly unallocated for an isolated recipe (§1 invariant).
    assert cmp.unallocated_leftover_value == cmp.provider.total_leftover_value


def test_optional_counted_only_on_baseline_blocks_comparison(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provider(db_session)
    _baseline(db_session)
    recipe = _recipe(db_session)

    real_cost_baseline = rs._cost_baseline

    def _baseline_including_miel(db, rec, slug) -> BaselineCosting:
        result = real_cost_baseline(db, rec, slug)
        result.optional_ingredients_included = ["miel"]  # baseline unilaterally counts an optional
        result.optional_ingredients_excluded = []
        return result

    monkeypatch.setattr(rs, "_cost_baseline", _baseline_including_miel)

    cmp = compare_recipe_shadow(db_session, recipe, _PROV, baseline_slug=_BASE)

    assert cmp.comparison_status == RecipeShadowStatus.OPTIONAL_INGREDIENT_MISMATCH.value
    assert cmp.absolute_difference is None  # money stays null on an invalid comparison
    assert cmp.provider_cost is None
