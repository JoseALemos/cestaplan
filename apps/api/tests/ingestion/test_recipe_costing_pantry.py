"""Pantry policy + purchased/consumed/leftover separation (audit §3/§4) — DB-backed, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
)
from cestaplan_api.services.recipe_costing import PantryPolicy, cost_recipe

_NOW = datetime.now(UTC)
_PROV = "test-pantry-prov"
_AVENA, _LECHE, _PLATANO, _MIEL = 792, 779, 760, 823


def _setup(db: Session) -> Recipe:
    r = Retailer(slug=_PROV, name="Pantry Prov", adapter_key="test", is_synthetic=True)
    db.add(r)
    db.flush()
    rid = r.id
    for ing_id, key, name, qty, unit, price in [
        (_AVENA, "avena_copos", "Copos 500g", "500", "g", "0.75"),
        (_LECHE, "leche_entera", "Leche 1L", "1000", "ml", "0.95"),
        (_PLATANO, "platano", "Plátano 700g", "700", "g", "2.49"),
    ]:
        product = Product(name=name, is_synthetic=False)
        db.add(product)
        db.flush()
        ext = ExternalProduct(retailer_id=rid, external_id=f"EXT-{name[:16]}")
        db.add(ext)
        db.flush()
        v = ProductVariant(
            retailer_id=rid,
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
                retailer_id=rid,
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
                ingredient_id=ing_id,
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
    recipe = Recipe(origin="seed", title="Pantry Recipe", servings=2, is_synthetic=True)
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
                ingredient_id=ing_id,
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


def test_three_money_concepts_are_separate(db_session: Session) -> None:
    recipe = _setup(db_session)
    r = cost_recipe(db_session, recipe, _PROV)
    assert r.fully_costable is True
    # purchased (full packages) > consumed (proportional) ; leftover = purchased - consumed value.
    assert r.total_purchase_cost == Decimal("4.19")
    assert r.total_consumed_cost == Decimal("1.07")
    assert r.total_leftover_value == Decimal("3.12")
    assert r.total_purchase_cost != r.total_consumed_cost  # never conflated
    # Per-serving is exposed for both bases.
    assert r.cost_per_serving_purchase == Decimal("2.10")
    assert r.cost_per_serving_consumed == Decimal("0.54")


def test_isolated_leftover_is_not_amortized(db_session: Session) -> None:
    recipe = _setup(db_session)
    r = cost_recipe(db_session, recipe, _PROV, pantry_policy=PantryPolicy.EMPTY_PANTRY)
    # For a single recipe leftover is neither assumed reused nor wasted -> wholly UNALLOCATED.
    assert r.reusable_leftover_value == Decimal("0.00")
    assert r.non_reusable_leftover_value == Decimal("0.00")
    assert r.unallocated_leftover_value == r.total_leftover_value
    assert r.total_leftover_value == Decimal("3.12")  # the surplus value is still reported


def test_leftover_accounting_invariant_holds(db_session: Session) -> None:
    recipe = _setup(db_session)
    r = cost_recipe(db_session, recipe, _PROV)
    # A fully costable recipe populates every leftover field (narrow for the type checker).
    assert r.reusable_leftover_value is not None
    assert r.non_reusable_leftover_value is not None
    assert r.unallocated_leftover_value is not None
    # leftover_value = reusable + non_reusable + unallocated (computed, not hard-coded).
    assert r.total_leftover_value == (
        r.reusable_leftover_value + r.non_reusable_leftover_value + r.unallocated_leftover_value
    )


def test_optional_is_excluded_from_the_costed_basket(db_session: Session) -> None:
    recipe = _setup(db_session)
    r = cost_recipe(db_session, recipe, _PROV)
    assert r.optional_ingredients_excluded == ["miel"]
    assert r.optional_ingredients_included == []
    # miel is unmapped here, so excluding it must not change the mandatory-only total.
    assert r.total_purchase_cost == Decimal("4.19")


def test_each_pantry_policy_is_recorded(db_session: Session) -> None:
    recipe = _setup(db_session)
    for policy in (
        PantryPolicy.EMPTY_PANTRY,
        PantryPolicy.USE_EXISTING_STOCK,
        PantryPolicy.PLAN_SHARED_INVENTORY,
    ):
        r = cost_recipe(db_session, recipe, _PROV, pantry_policy=policy)
        assert r.pantry_policy == policy.value
        # Purchased outlay for a single isolated recipe is the full-package cost under every policy
        # (no real inventory/plan is supplied here to amortize against).
        assert r.total_purchase_cost == Decimal("4.19")
