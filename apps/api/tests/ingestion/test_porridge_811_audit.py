"""Final audit of the real recipe (id=811) 'Porridge de avena con plátano' (§5) — DB-backed.

Values are COMPUTED from the live costing/shadow (not hard-coded domain constants) and checked.
If the Alcampo discovery data is absent (a fresh DB), the test skips rather than asserting on
missing state — it never fabricates data and never touches production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.models import ProductVariant, Recipe
from cestaplan_api.services.recipe_costing import cost_recipe
from cestaplan_api.services.recipe_shadow import compare_recipe_shadow

_RECIPE_ID = 811
_PROVIDER = "parsebot-alcampo"


def _recipe(db: Session) -> Recipe:
    recipe = db.get(Recipe, _RECIPE_ID)
    if recipe is None:
        pytest.skip("recipe 811 not present in this database")
    return recipe


def test_recipe_811_identity_is_unaltered(db_session: Session) -> None:
    recipe = _recipe(db_session)
    assert "orridge" in recipe.title
    assert recipe.servings == 2
    mandatory = {ri.canonical_name for ri in recipe.ingredients if not ri.optional}
    assert mandatory == {"avena_copos", "leche_entera", "platano"}
    optional = {ri.canonical_name for ri in recipe.ingredients if ri.optional}
    assert "miel" in optional  # miel stays optional


def test_recipe_811_is_fully_costable_with_evidence(db_session: Session) -> None:
    recipe = _recipe(db_session)
    costing = cost_recipe(db_session, recipe, _PROVIDER)
    if not costing.fully_costable:
        pytest.skip("Alcampo discovery data not present; recipe 811 not costable here")

    # Plátano is a genuine fixed package with 700 g / 2.49 € evidence.
    platano = next(line for line in costing.lines if line.canonical_name == "platano")
    assert platano.costing_mode == "fixed_package"
    assert platano.package_quantity == Decimal("700.0000")
    assert platano.package_unit == "g"
    assert platano.package_price == Decimal("2.49")

    # Money concepts (computed, then asserted).
    assert costing.total_purchase_cost == Decimal("4.19")
    assert costing.total_consumed_cost == Decimal("1.07")
    assert costing.total_leftover_value == Decimal("3.12")
    # Accounting invariant + unallocated leftover for an isolated recipe.
    assert costing.total_leftover_value == (
        costing.reusable_leftover_value
        + costing.non_reusable_leftover_value
        + costing.unallocated_leftover_value
    )
    assert costing.unallocated_leftover_value == Decimal("3.12")

    # Per-serving is derived from the purchased cost and correctly rounded.
    expected_per_serving = (costing.total_purchase_cost / Decimal(recipe.servings)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    assert costing.cost_per_serving_purchase == expected_per_serving == Decimal("2.10")

    # Miel is optional and excluded symmetrically.
    assert costing.optional_ingredients_excluded == ["miel"]
    assert costing.optional_ingredients_included == []


def test_recipe_811_shadow_is_comparable(db_session: Session) -> None:
    recipe = _recipe(db_session)
    cmp = compare_recipe_shadow(db_session, recipe, _PROVIDER)
    if cmp.comparison_status != "comparable":
        pytest.skip(f"shadow not comparable in this database ({cmp.comparison_status})")
    assert cmp.provider_input_fingerprint == cmp.baseline_input_fingerprint
    assert cmp.comparison_input_fingerprint == cmp.provider_input_fingerprint
    assert cmp.provider_cost == Decimal("4.19")
    assert cmp.unallocated_leftover_value == cmp.provider.total_leftover_value


def test_recipe_811_mappings_are_invisible_to_production(db_session: Session) -> None:
    recipe = _recipe(db_session)
    costing = cost_recipe(db_session, recipe, _PROVIDER)
    if not costing.fully_costable:
        pytest.skip("Alcampo discovery data not present")
    prices = CurrentPriceService()
    for line in costing.lines:
        if not line.costable or line.product_id is None:
            continue
        variants = (
            db_session.execute(
                select(ProductVariant).where(ProductVariant.product_id == line.product_id)
            )
            .scalars()
            .all()
        )
        for v in variants:
            # Staging price exists; the PRODUCTION view must never see it.
            assert (
                prices.current(db_session, v.id, as_of=datetime.now(UTC), staging=False) is None
            )
