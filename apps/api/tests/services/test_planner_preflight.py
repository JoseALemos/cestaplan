"""Deterministic planner preflight + infeasibility enrichment (pure, no DB, no network).

The optimizer must never run on an impossible precondition, and an empty catalogue must never be
reported as a budget problem.
"""

from __future__ import annotations

from cestaplan_api.services.catalog_readiness import ReadinessStatus, _global_status
from cestaplan_api.services.plan_service import _enrich_infeasibility
from cestaplan_api.services.planner_preflight import (
    ActionCode,
    PreflightCode,
    evaluate,
)

_OK = {
    "recipes_active": 5,
    "retailer_selected": False,
    "approved_mappings": 10,
    "productive_prices": 20,
    "costable_recipes": 5,
    "requested_meal_types": 3,
}


def test_zero_recipes_is_no_active_recipes_never_budget() -> None:
    out = evaluate(**{**_OK, "recipes_active": 0})
    assert out.ok is False
    assert out.code is PreflightCode.NO_ACTIVE_RECIPES
    # High budget cannot hide an empty catalogue: the report carries no budget at all.
    report = out.to_report()
    assert report["minimum_budget"] is None
    assert report["offending_products"] == []
    assert report["code"] == "no_active_recipes"
    # Only the add_recipes action — never a budget action.
    assert report["suggested_actions"] == ["add_recipes"]


def test_recipes_but_no_mappings_is_no_mapped_products() -> None:
    out = evaluate(**{**_OK, "approved_mappings": 0})
    assert out.code is PreflightCode.NO_MAPPED_PRODUCTS


def test_mappings_but_no_prices_is_no_product_prices() -> None:
    out = evaluate(**{**_OK, "productive_prices": 0})
    assert out.code is PreflightCode.NO_PRODUCT_PRICES


def test_selected_retailer_without_catalog() -> None:
    out = evaluate(**{**_OK, "retailer_selected": True, "productive_prices": 0})
    assert out.code is PreflightCode.RETAILER_WITHOUT_CATALOG


def test_prices_but_nothing_costable_is_no_costable_recipes() -> None:
    out = evaluate(**{**_OK, "costable_recipes": 0})
    assert out.code is PreflightCode.NO_COSTABLE_RECIPES


def test_too_few_costable_for_requested_variety() -> None:
    out = evaluate(**{**_OK, "costable_recipes": 2, "requested_meal_types": 3})
    assert out.code is PreflightCode.INSUFFICIENT_RECIPE_VARIETY


def test_healthy_catalogue_passes_preflight() -> None:
    out = evaluate(**_OK)
    assert out.ok is True
    assert out.code is None


def test_preflight_never_emits_a_budget_code() -> None:
    # No combination of empty-catalogue inputs yields genuine_budget_infeasibility.
    for override in ({"recipes_active": 0}, {"approved_mappings": 0}, {"productive_prices": 0}):
        out = evaluate(**{**_OK, **override})
        assert out.code is not PreflightCode.GENUINE_BUDGET_INFEASIBILITY
    # Suggested actions are the typed ActionCode vocabulary.
    for a in evaluate(**{**_OK, "recipes_active": 0}).suggested_actions:
        assert isinstance(a, ActionCode)


# --------------------------------------------------------------------------- #
# Engine-infeasibility enrichment (budget path)
# --------------------------------------------------------------------------- #


def test_enrich_budget_conflict_becomes_genuine_budget_infeasibility() -> None:
    report = _enrich_infeasibility(
        {
            "status": "infeasible",
            "minimal_conflict": ["budget:2.00 EUR", "meals:5", "store_catalog_prices"],
            "min_budget_found": "8.40",
            "suggested_actions": ["raise_budget_to:8.40 EUR", "reduce_meals", "change_store"],
        }
    )
    assert report["code"] == "genuine_budget_infeasibility"
    assert report["minimum_budget"] == "8.40"
    assert "increase_budget" in report["suggested_actions"]
    assert "reduce_meals" in report["suggested_actions"]


def test_enrich_missing_candidate_is_not_budget() -> None:
    report = _enrich_infeasibility(
        {
            "status": "infeasible",
            "minimal_conflict": ["no_candidate_for:lunch"],
            "min_budget_found": None,
            "suggested_actions": ["add_recipes", "relax_soft_preferences"],
        }
    )
    assert report["code"] == "no_compatible_recipes"
    assert report["minimum_budget"] is None
    assert "increase_budget" not in report["suggested_actions"]


def test_enrich_hard_constraint_conflict() -> None:
    report = _enrich_infeasibility(
        {
            "status": "infeasible",
            "minimal_conflict": ["hard_constraint:gluten"],
            "min_budget_found": None,
            "suggested_actions": ["add_recipes"],
        }
    )
    assert report["code"] == "hard_constraints_infeasible"


# --------------------------------------------------------------------------- #
# Catalog readiness global status (pure)
# --------------------------------------------------------------------------- #

_READY = {
    "recipes_active": 10,
    "productive_products": 50,
    "staging_products": 0,
    "staging_observations": 0,
    "approved_mappings": 40,
    "productive_prices": 60,
    "recipes_costable": 8,
    "production_ready_providers": 0,
}


def test_readiness_status_ladder() -> None:
    assert _global_status(**{**_READY, "recipes_active": 0}) is ReadinessStatus.NO_RECIPES
    assert (
        _global_status(**{**_READY, "productive_products": 0, "productive_prices": 0})
        is ReadinessStatus.NO_CATALOG
    )
    assert (
        _global_status(**{**_READY, "approved_mappings": 0}) is ReadinessStatus.PENDING_MAPPINGS
    )
    assert (
        _global_status(**{**_READY, "productive_prices": 0, "staging_observations": 5})
        is ReadinessStatus.STAGING_ONLY
    )
    assert (
        _global_status(**{**_READY, "productive_prices": 0}) is ReadinessStatus.NO_PRICES
    )
    assert (
        _global_status(**{**_READY, "recipes_costable": 0}) is ReadinessStatus.PENDING_MAPPINGS
    )
    assert _global_status(**_READY) is ReadinessStatus.READY_FOR_REVIEW


def test_readiness_never_available_without_production_approved_provider() -> None:
    # Fully costable catalogue but no production-approved provider -> never "available".
    assert _global_status(**_READY) is not ReadinessStatus.AVAILABLE
    assert (
        _global_status(**{**_READY, "production_ready_providers": 1}) is ReadinessStatus.AVAILABLE
    )
