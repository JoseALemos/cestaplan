"""Deterministic price fallback (spec §Y) + variant costing classifier — pure, no DB, no network."""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.ingestion.providers.contracts import ProductCostingMode
from cestaplan_api.ingestion.providers.onboarding import classify_variant_costing_mode
from cestaplan_api.services.price_fallback import (
    FallbackAction,
    FallbackCandidate,
    Freshness,
    IngredientNeed,
    resolve_with_fallback,
)


def _need(**over: object) -> IngredientNeed:
    base: dict[str, object] = {
        "ingredient_id": 1,
        "canonical_name": "leche",
        "quantity": Decimal("1"),
        "unit": "l",
        "required_scope": "national",
        "original_product_id": 100,
        "original_net_content_signature": "1000ml",
    }
    base.update(over)
    return IngredientNeed(**base)  # type: ignore[arg-type]


def _cand(**over: object) -> FallbackCandidate:
    base: dict[str, object] = {
        "product_id": 100,
        "variant_id": 10,
        "canonical_name": "leche",
        "brand": None,
        "costing_mode": ProductCostingMode.FIXED_PACKAGE,
        "price": Decimal("0.95"),
        "price_scope": "national",
        "net_content_signature": "1000ml",
    }
    base.update(over)
    return FallbackCandidate(**base)  # type: ignore[arg-type]


def test_alternate_variant_same_product() -> None:
    d = resolve_with_fallback(_need(), [_cand(variant_id=11)])
    assert d.action is FallbackAction.ALTERNATE_VARIANT
    assert d.replacement_cost == Decimal("0.95") and d.confidence > Decimal("0.9")


def test_canonical_equivalent_other_product() -> None:
    d = resolve_with_fallback(_need(), [_cand(product_id=200, variant_id=20)])
    assert d.action is FallbackAction.CANONICAL_EQUIVALENT
    assert d.selected_product_id == 200


def test_alternate_package_size() -> None:
    d = resolve_with_fallback(_need(), [_cand(product_id=200, net_content_signature="500ml")])
    assert d.action is FallbackAction.ALTERNATE_PACKAGE


def test_alternate_brand() -> None:
    d = resolve_with_fallback(
        _need(), [_cand(product_id=200, net_content_signature="500ml", brand="Hacendado")]
    )
    # a package difference is preferred over a brand switch; force brand-only by matching package
    d2 = resolve_with_fallback(_need(), [_cand(product_id=200, brand="Hacendado")])
    assert d.action in (FallbackAction.ALTERNATE_PACKAGE, FallbackAction.ALTERNATE_BRAND)
    assert d2.action is FallbackAction.ALTERNATE_BRAND
    assert any("brand substituted" in w for w in d2.warnings)


def test_dietary_substitution_allowed() -> None:
    need = _need(canonical_name="leche", dietary_constraints=frozenset())
    sub = _cand(
        product_id=300,
        canonical_name="bebida_avena",
        same_canonical=False,
        is_substitution=True,
        diet_tags=frozenset({"vegano"}),
    )
    d = resolve_with_fallback(need, [sub])
    assert d.action is FallbackAction.DIETARY_SUBSTITUTION


def test_allergy_blocks_substitution() -> None:
    need = _need(household_allergens=frozenset({"gluten"}))
    sub = _cand(
        product_id=300,
        canonical_name="pan",
        same_canonical=False,
        is_substitution=True,
        allergens=frozenset({"gluten"}),
    )
    d = resolve_with_fallback(need, [sub])
    # the allergenic substitute is never selected -> falls through to no verified solution
    assert d.action is FallbackAction.NO_VERIFIED_SOLUTION
    assert d.allergens_checked is True


def test_diet_blocks_substitution() -> None:
    need = _need(dietary_constraints=frozenset({"carne"}))
    sub = _cand(
        product_id=300,
        canonical_name="picada",
        same_canonical=False,
        is_substitution=True,
        diet_tags=frozenset({"carne"}),
    )
    d = resolve_with_fallback(need, [sub])
    assert d.action is FallbackAction.NO_VERIFIED_SOLUTION


def test_old_price_requires_user_approval() -> None:
    stale = _cand(freshness=Freshness.STALE)
    d = resolve_with_fallback(_need(), [stale], allow_stale=False)
    assert d.action is FallbackAction.USER_APPROVAL_REQUIRED
    assert any("approval" in w for w in d.warnings)
    # ...but if approval is granted, it becomes usable again.
    d2 = resolve_with_fallback(_need(), [stale], allow_stale=True)
    assert d2.action is FallbackAction.ALTERNATE_VARIANT


def test_incompatible_scope_is_not_used() -> None:
    need = _need(required_scope="exact_store")
    far = _cand(price_scope="national")
    d = resolve_with_fallback(need, [far])
    # national is broader than exact_store -> not usable -> partial (a priced candidate exists)
    assert d.action is FallbackAction.PARTIAL_COST


def test_partial_cost_when_only_unresolved_package() -> None:
    bad = _cand(costing_mode=ProductCostingMode.UNRESOLVED)
    d = resolve_with_fallback(_need(), [bad])
    assert d.action is FallbackAction.PARTIAL_COST
    assert any("PARTIAL" in w for w in d.warnings)


def test_recipe_regeneration_when_no_priced_candidate() -> None:
    unpriced = _cand(price=None)
    d = resolve_with_fallback(_need(), [unpriced])
    assert d.action is FallbackAction.RECIPE_REGENERATION_REQUIRED


def test_no_verified_solution_when_empty() -> None:
    d = resolve_with_fallback(_need(), [])
    assert d.action is FallbackAction.NO_VERIFIED_SOLUTION
    assert d.confidence == Decimal("0")


def test_never_uses_zero_or_negative_price() -> None:
    zero = _cand(price=Decimal("0"))
    neg = _cand(price=Decimal("-1"))
    assert (
        resolve_with_fallback(_need(), [zero]).action is FallbackAction.RECIPE_REGENERATION_REQUIRED
    )
    assert (
        resolve_with_fallback(_need(), [neg]).action is FallbackAction.RECIPE_REGENERATION_REQUIRED
    )


# --- variant costing classifier (used by coverage/shadow) --------------------------------- #
def test_valid_weight_product_is_costable() -> None:
    mode = classify_variant_costing_mode(
        sell_unit="weight",
        variable_weight=True,
        net_content_quantity=None,
        net_content_unit=None,
        unit_price=Decimal("9.9"),
        unit_price_unit="kg",
        has_price=True,
    )
    assert mode is ProductCostingMode.VARIABLE_WEIGHT


def test_incomplete_fixed_package_is_unresolved() -> None:
    mode = classify_variant_costing_mode(
        sell_unit="package",
        variable_weight=False,
        net_content_quantity=None,
        net_content_unit=None,
        unit_price=Decimal("4.0"),
        unit_price_unit="kg",
        has_price=True,
    )
    assert mode is ProductCostingMode.UNRESOLVED


def test_no_price_is_unresolved() -> None:
    mode = classify_variant_costing_mode(
        sell_unit="package",
        variable_weight=False,
        net_content_quantity=Decimal("500"),
        net_content_unit="g",
        unit_price=None,
        unit_price_unit=None,
        has_price=False,
    )
    assert mode is ProductCostingMode.UNRESOLVED
