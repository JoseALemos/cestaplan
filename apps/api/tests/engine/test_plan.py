"""End-to-end deterministic plan generation (OPTIMIZATION.md, acceptance-critical)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_engine import generate_plan
from cestaplan_engine.contracts import InfeasibleResult, PlanResult

from .builders import (
    ingredient,
    member,
    package,
    plan_input,
    product,
    recipe,
    requirement,
)

CHICKEN = product(
    "chicken_500", "chicken", [package("chicken_500", "500", "g", "4.20")], category="meat"
)
RICE = product(
    "rice_1kg", "rice", [package("rice_1kg", "1000", "g", "2.00")], category="grains"
)
OATS = product(
    "oats_500", "oats", [package("oats_500", "500", "g", "1.50")], category="cereals"
)
APPLE = product(
    "apple_unit", "apple", [package("apple_unit", "1", "unit", "0.30")], category="fruit"
)


def _chicken_lunch(rid="lunch1", qty="600"):
    return recipe(rid, {"lunch"}, [ingredient("chicken", qty, "g")], servings=2)


# --------------------------------------------------------------------------- #
def test_money_is_decimal_and_exact_sum():
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1)],
            catalog=[CHICKEN],
            candidates=[_chicken_lunch()],
        )
    )
    assert isinstance(res, PlanResult)
    assert isinstance(res.cost_total.total, Decimal)
    assert res.cost_total.total == Decimal("8.40")
    assert res.cost_total.known == Decimal("8.40")
    line = res.grocery_lines[0]
    assert line.packages_count == 2
    assert line.purchased_quantity == Decimal("1000")
    assert line.used_quantity == Decimal("600")
    assert line.leftover == Decimal("400")
    assert line.subtotal == Decimal("8.40")


def test_marginal_cost_when_product_shared():
    lunch = recipe("lunch1", {"lunch"}, [ingredient("chicken", "600", "g")], servings=2)
    dinner = recipe("dinner1", {"dinner"}, [ingredient("chicken", "300", "g")], servings=2)
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1), requirement("dinner", 1)],
            catalog=[CHICKEN],
            candidates=[lunch, dinner],
        )
    )
    assert isinstance(res, PlanResult)
    # 900 g total -> still only 2 packs -> 8.40 total.
    assert res.cost_total.total == Decimal("8.40")
    by_type = {m.meal_type: m for m in res.planned_meals}
    # First meal (lunch) pays the full packs; dinner reuses the leftover -> marginal 0.
    assert by_type["lunch"].cost.marginal == Decimal("8.40")
    assert by_type["dinner"].cost.marginal == Decimal("0")


def test_pantry_reduces_purchase_in_plan():
    from cestaplan_engine.contracts import PantryItemDTO

    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1)],
            catalog=[CHICKEN],
            candidates=[_chicken_lunch()],
            pantry=[PantryItemDTO(canonical_name="chicken", quantity=Decimal("200"), unit="g")],
        )
    )
    assert isinstance(res, PlanResult)
    line = res.grocery_lines[0]
    assert line.pantry_quantity == Decimal("200")
    assert line.pending_quantity == Decimal("400")
    assert line.packages_count == 1
    assert res.cost_total.total == Decimal("4.20")
    assert res.pantry_used


def test_allergen_recipe_never_scheduled():
    gluten = recipe("bad", {"lunch"}, [ingredient("pasta", "200", "g")], allergens={"gluten"})
    safe = recipe("good", {"lunch"}, [ingredient("rice", "200", "g")])
    res = generate_plan(
        plan_input(
            members=[member("A", allergens={"gluten"})],
            requirements=[requirement("lunch", 1)],
            catalog=[RICE],
            candidates=[gluten, safe],
        )
    )
    assert isinstance(res, PlanResult)
    assert all(m.recipe_id == "good" for m in res.planned_meals)


def test_no_safe_candidate_is_infeasible():
    gluten = recipe("bad", {"lunch"}, [ingredient("pasta", "200", "g")], allergens={"gluten"})
    res = generate_plan(
        plan_input(
            members=[member("A", allergens={"gluten"})],
            requirements=[requirement("lunch", 1)],
            catalog=[RICE],
            candidates=[gluten],
        )
    )
    assert isinstance(res, InfeasibleResult)
    assert any("no_candidate_for:lunch" in c for c in res.minimal_conflict)


def test_expired_price_flagged_in_plan():
    expired_product = product(
        "chicken_500",
        "chicken",
        [package("chicken_500", "500", "g", "4.20", expires_at=date(2026, 7, 1))],
    )
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1)],
            catalog=[expired_product],
            candidates=[_chicken_lunch()],
        )
    )
    assert isinstance(res, PlanResult)
    assert res.coverage.status == "stale"
    assert res.coverage.counts.expired == 1
    # Expired price is not counted as known.
    assert res.cost_total.known == Decimal("0")
    assert res.cost_total.estimated == Decimal("8.40")
    assert any("expired" in w for w in res.warnings)


def test_impossible_budget_returns_infeasible_not_fake_plan():
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1)],
            catalog=[CHICKEN],
            candidates=[_chicken_lunch()],
            budget_amount="2.00",
        )
    )
    assert isinstance(res, InfeasibleResult)
    assert res.min_budget_found == Decimal("8.40")
    assert any(o.canonical_name == "chicken" for o in res.offending_products)
    assert any("raise_budget_to" in a for a in res.suggested_actions)


def test_full_ten_meal_plan_completes():
    catalog = [CHICKEN, RICE, OATS, APPLE]
    candidates = [
        recipe("bf1", {"breakfast"}, [ingredient("oats", "80", "g")]),
        recipe("bf2", {"breakfast"}, [ingredient("oats", "100", "g")]),
        recipe("lu1", {"lunch"}, [ingredient("chicken", "300", "g")]),
        recipe("lu2", {"lunch"}, [ingredient("rice", "200", "g")]),
        recipe("sn1", {"snack"}, [ingredient("apple", "2", "unit")]),
        recipe("di1", {"dinner"}, [ingredient("rice", "250", "g")]),
        recipe("di2", {"dinner"}, [ingredient("chicken", "250", "g")]),
    ]
    reqs = [
        requirement("breakfast", 2),
        requirement("lunch", 4),
        requirement("snack", 1),
        requirement("dinner", 3),
    ]
    res = generate_plan(
        plan_input(
            members=[member("A"), member("B")],
            requirements=reqs,
            catalog=catalog,
            candidates=candidates,
            budget_amount="500",
        )
    )
    assert isinstance(res, PlanResult)
    assert len(res.planned_meals) == 10
    assert len(res.explanations) == 10
    assert isinstance(res.cost_total.total, Decimal)


def test_reproducible_same_seed_same_output():
    catalog = [CHICKEN, RICE, OATS, APPLE]
    candidates = [
        recipe("bf1", {"breakfast"}, [ingredient("oats", "80", "g")]),
        recipe("bf2", {"breakfast"}, [ingredient("oats", "100", "g")]),
        recipe("lu1", {"lunch"}, [ingredient("chicken", "300", "g")]),
        recipe("lu2", {"lunch"}, [ingredient("rice", "200", "g")]),
        recipe("sn1", {"snack"}, [ingredient("apple", "2", "unit")]),
        recipe("di1", {"dinner"}, [ingredient("rice", "250", "g")]),
        recipe("di2", {"dinner"}, [ingredient("chicken", "250", "g")]),
    ]
    reqs = [
        requirement("breakfast", 2),
        requirement("lunch", 4),
        requirement("snack", 1),
        requirement("dinner", 3),
    ]

    def run():
        return generate_plan(
            plan_input(
                members=[member("A"), member("B")],
                requirements=reqs,
                catalog=catalog,
                candidates=candidates,
                budget_amount="500",
                seed=7,
            )
        )

    first = run()
    second = run()
    assert first.model_dump_json() == second.model_dump_json()
