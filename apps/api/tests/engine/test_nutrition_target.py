"""Nutrition-target fitting: the optimizer trends toward the household goal.

A household with a per-day protein target and a mix of high/low-protein candidates
(comfortable budget) should get a plan skewed toward the high-protein recipes vs a
run with no target — without breaking budget/allergen/variety guarantees. When no
target is set the plan and result are unchanged (reproducibility preserved).
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine import generate_plan
from cestaplan_engine.contracts import (
    NutritionDTO,
    NutritionTargetDTO,
    PlanResult,
    ScoringWeights,
)

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
    # actual_per_day = one serving per meal * comensales / days, independent of batch
    # size. Here meals are batch-cooked in 2 servings (per-serving = half the displayed
    # batch nutrition) and there is 1 comensal, so it is half the batch total per day.
    expected_per_day = (_total_protein(res) / Decimal("2")) / Decimal(summary.days)
    assert protein.actual_per_day == expected_per_day
    assert protein.deviation == protein.actual_per_day - Decimal("90")
    assert protein.coverage_ratio == protein.actual_per_day / Decimal("90")
    assert protein.status in ("met", "under", "over")

    # A macro with no target reads as "unknown" with no target_per_day.
    assert summary.fat_g.status == "unknown"
    assert summary.fat_g.target_per_day is None


# --- servings/comensales coupling: batch size must not distort the nutrition summary ---
def _forced_input(target, *, servings: int, members):
    """Exactly _SLOTS distinct high-protein recipes -> the assignment is forced, so
    only ``servings`` (batch size) and ``members`` (comensales) vary between runs."""
    cands = [_high(f"only{i}") for i in range(_SLOTS)]
    return plan_input(
        members=members,
        requirements=[requirement("lunch", _SLOTS, servings=servings)],
        catalog=[CHICKEN, RICE],
        candidates=cands,
        budget_amount="1000",
        nutrition_target=target,
    )


def test_nutrition_summary_independent_of_batch_size():
    """Cooking 8 servings/meal instead of 2 (batch for leftovers) must not change the
    per-day nutrition summary: the target is about comensales, not portions cooked."""
    target = NutritionTargetDTO(protein_g=Decimal("90"))
    small = generate_plan(_forced_input(target, servings=2, members=[member("A")]))
    big = generate_plan(_forced_input(target, servings=8, members=[member("A")]))
    assert isinstance(small, PlanResult) and isinstance(big, PlanResult)

    assert small.nutrition_summary is not None and big.nutrition_summary is not None
    assert (
        small.nutrition_summary.protein_g.actual_per_day
        == big.nutrition_summary.protein_g.actual_per_day
    )
    # Sanity: per-meal displayed nutrition still reflects the cooked batch (4x here).
    assert _total_protein(big) == _total_protein(small) * Decimal("4")


def test_nutrition_summary_scales_with_comensales():
    """Two comensales eat twice the household total of one -> actual_per_day doubles."""
    target = NutritionTargetDTO(protein_g=Decimal("90"))
    one = generate_plan(_forced_input(target, servings=2, members=[member("A")]))
    two = generate_plan(
        _forced_input(target, servings=2, members=[member("A"), member("B")])
    )
    assert isinstance(one, PlanResult) and isinstance(two, PlanResult)
    assert one.nutrition_summary is not None and two.nutrition_summary is not None
    assert (
        two.nutrition_summary.protein_g.actual_per_day
        == one.nutrition_summary.protein_g.actual_per_day * Decimal("2")
    )


# --- the "nutrition" priority: a strong nutrition weight overrides competing terms ------------
# Same high/low-protein recipes, but now the low-protein recipe is FAR faster to cook (10 vs 60
# min). Under the default nutrition weight (1.2) that big time edge makes the fast low-protein
# plan win even with a protein target set; raising the weight to the variety level (12, the
# "nutrition" priority) makes fitting the protein target win despite the time cost.
_CHICKEN_SLOW = product(
    "chicken_300",
    "chicken",
    [package("chicken_300", "300", "g", "3.00")],
    category="meat",
    nutrition=NutritionDTO(protein_g=Decimal("30"), kcal=Decimal("150")),
)
_RICE_FAST = product(
    "rice_300",
    "rice",
    [package("rice_300", "300", "g", "1.50")],
    category="grains",
    nutrition=NutritionDTO(protein_g=Decimal("2"), kcal=Decimal("130")),
)


def _make_time_edge_input(weights: ScoringWeights | None):
    highs = [
        recipe(
            f"high{i}", {"lunch"}, [ingredient("chicken", "300", "g")],
            servings=2, prep=10, cook=60,
        )
        for i in range(_SLOTS)
    ]
    lows = [
        recipe(
            f"low{i}", {"lunch"}, [ingredient("rice", "300", "g")],
            servings=2, prep=10, cook=10,
        )
        for i in range(_SLOTS)
    ]
    return plan_input(
        members=[member("A")],
        requirements=[requirement("lunch", _SLOTS, servings=2)],
        catalog=[_CHICKEN_SLOW, _RICE_FAST],
        candidates=[*highs, *lows],
        budget_amount="100",
        nutrition_target=NutritionTargetDTO(protein_g=Decimal("90")),
        weights=weights,
    )


def test_nutrition_priority_weight_overrides_competing_terms():
    # Default weight: the time edge wins, so the plan carries little protein.
    default_plan = generate_plan(_make_time_edge_input(None))
    # "nutrition" priority weight (== variety driver): the protein target wins instead.
    priority_plan = generate_plan(
        _make_time_edge_input(ScoringWeights(nutrition_deviation=Decimal("12")))
    )
    assert isinstance(default_plan, PlanResult)
    assert isinstance(priority_plan, PlanResult)

    # The boosted nutrition weight yields a strictly higher-protein plan.
    assert _total_protein(priority_plan) > _total_protein(default_plan)

    # Hard guarantees untouched: still within budget, still fully varied (no repeats).
    assert priority_plan.cost_total.total <= Decimal("100")
    ids = [m.recipe_id for m in priority_plan.planned_meals]
    assert len(set(ids)) == len(ids) == _SLOTS


def test_no_target_leaves_result_unchanged_and_reproducible():
    first = generate_plan(_make_input(None))
    second = generate_plan(_make_input(None))
    assert isinstance(first, PlanResult)
    # No target -> no nutrition summary (behavior unchanged from before the feature).
    assert first.nutrition_summary is None
    # Deterministic: identical input + seed -> byte-identical serialized result.
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
