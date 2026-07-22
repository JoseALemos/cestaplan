"""Nutrition-target fitting: the optimizer trends toward the household goal.

A household with a per-day protein target and a mix of high/low-protein candidates
(comfortable budget) should get a plan skewed toward the high-protein recipes vs a
run with no target — without breaking budget/allergen/variety guarantees. When no
target is set the plan and result are unchanged (reproducibility preserved).
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine import generate_plan
from cestaplan_engine.contracts import NutritionDTO, NutritionTargetDTO, PlanResult

from .builders import ingredient, member, package, plan_input, product, recipe, requirement

# 300 g packages match the 300 g used per recipe, so leftover (and waste) is 0 for
# both options -> the only non-nutrition differentiator is a 1-minute time edge that
# makes the low-protein recipe win when there is no target.
CHICKEN = product(
    "chicken_300",
    "chicken",
    [package("chicken_300", "300", "g", "3.00")],
    category="meat",
    nutrition=NutritionDTO(protein_g=Decimal("30"), kcal=Decimal("150")),
)
RICE = product(
    "rice_300",
    "rice",
    [package("rice_300", "300", "g", "1.50")],
    category="grains",
    nutrition=NutritionDTO(protein_g=Decimal("2"), kcal=Decimal("130")),
)

_SLOTS = 4


def _high(rid: str):
    return recipe(rid, {"lunch"}, [ingredient("chicken", "300", "g")], servings=2, prep=10, cook=11)


def _low(rid: str):
    return recipe(rid, {"lunch"}, [ingredient("rice", "300", "g")], servings=2, prep=10, cook=10)


def _candidates():
    highs = [_high(f"high{i}") for i in range(_SLOTS)]
    lows = [_low(f"low{i}") for i in range(_SLOTS)]
    return [*highs, *lows]


def _total_protein(res: PlanResult) -> Decimal:
    total = Decimal("0")
    for meal in res.planned_meals:
        if meal.nutrition and meal.nutrition.protein_g is not None:
            total += meal.nutrition.protein_g
    return total


def _make_input(target: NutritionTargetDTO | None):
    return plan_input(
        members=[member("A")],
        requirements=[requirement("lunch", _SLOTS, servings=2)],
        catalog=[CHICKEN, RICE],
        candidates=_candidates(),
        budget_amount="100",
        nutrition_target=target,
    )


def test_protein_target_pulls_plan_toward_high_protein():
    no_target = generate_plan(_make_input(None))
    with_target = generate_plan(_make_input(NutritionTargetDTO(protein_g=Decimal("90"))))

    assert isinstance(no_target, PlanResult)
    assert isinstance(with_target, PlanResult)

    # With a protein target the plan carries strictly more protein than without.
    assert _total_protein(with_target) > _total_protein(no_target)

    # Variety guarantee still holds: no recipe repeats (4 distinct dishes for 4 slots).
    ids = [m.recipe_id for m in with_target.planned_meals]
    assert len(set(ids)) == len(ids) == _SLOTS

    # Budget guarantee still holds.
    assert with_target.cost_total.total <= Decimal("100")


def test_nutrition_summary_present_and_correct_with_target():
    res = generate_plan(_make_input(NutritionTargetDTO(protein_g=Decimal("90"))))
    assert isinstance(res, PlanResult)
    summary = res.nutrition_summary
    assert summary is not None
    assert summary.days >= 1

    protein = summary.protein_g
    assert protein.target_per_day == Decimal("90")
    assert protein.actual_per_day is not None
    # actual_per_day == plan total protein / days (consistent with per-meal nutrition).
    expected_per_day = _total_protein(res) / Decimal(summary.days)
    assert protein.actual_per_day == expected_per_day
    assert protein.deviation == protein.actual_per_day - Decimal("90")
    assert protein.coverage_ratio == protein.actual_per_day / Decimal("90")
    assert protein.status in ("met", "under", "over")

    # A macro with no target reads as "unknown" with no target_per_day.
    assert summary.fat_g.status == "unknown"
    assert summary.fat_g.target_per_day is None


def test_no_target_leaves_result_unchanged_and_reproducible():
    first = generate_plan(_make_input(None))
    second = generate_plan(_make_input(None))
    assert isinstance(first, PlanResult)
    # No target -> no nutrition summary (behavior unchanged from before the feature).
    assert first.nutrition_summary is None
    # Deterministic: identical input + seed -> byte-identical serialized result.
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
