"""Single deterministic entrypoint: ``generate_plan`` (OPTIMIZATION.md §1).

Orchestrates the pipeline — schedule -> hard-filter -> optimize -> provision ->
cost/coverage/nutrition -> explain — and returns either a :class:`PlanResult` or,
when no feasible plan exists, an :class:`InfeasibleResult`. Pure: it reads the
``as_of`` date from the input and never calls ``datetime.now`` or unseeded random.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import cast

from cestaplan_engine.contracts import (
    CandidateRecipeDTO,
    CostBreakdown,
    CostTotal,
    InfeasibleResult,
    MealType,
    PlanInput,
    PlannedMealDTO,
    PlanResult,
)
from cestaplan_engine.explain import ConstraintExplainer, explain_meal
from cestaplan_engine.matching import ProductMatcher
from cestaplan_engine.nutrition import NutritionCalculator
from cestaplan_engine.optimizer import PlanOptimizer
from cestaplan_engine.packaging import PackageOptimizer
from cestaplan_engine.pantry import PantryCalculator
from cestaplan_engine.pricing import compute_coverage
from cestaplan_engine.provisioning import MealAssignment, Provisioner
from cestaplan_engine.scheduling import MealScheduler, MealSlot
from cestaplan_engine.units import UnitConverter
from cestaplan_engine.validators import AllergenValidator, DietaryRestrictionValidator


def _substitution_groups(candidates: list[CandidateRecipeDTO]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for recipe in candidates:
        for ing in recipe.ingredients:
            if ing.substitution_group and ing.canonical_name not in groups[
                ing.substitution_group
            ]:
                groups[ing.substitution_group].append(ing.canonical_name)
    return dict(groups)


def generate_plan(plan_input: PlanInput) -> PlanResult | InfeasibleResult:
    """Deterministically build a meal plan, or explain why none is possible."""
    converter = UnitConverter(plan_input.conversions)
    matcher = ProductMatcher(
        plan_input.catalog, _substitution_groups(plan_input.candidates)
    )
    pantry_calc = PantryCalculator(plan_input.pantry, converter, plan_input.as_of)
    packager = PackageOptimizer()
    provisioner = Provisioner(
        matcher, pantry_calc, packager, plan_input.weights, converter, plan_input.as_of
    )

    scheduler = MealScheduler()
    slots = scheduler.schedule(plan_input.meal_requirements, plan_input.date_range)

    warnings: list[str] = []

    # --- hard-constraint filtering (allergens + dietary + equipment) -------- #
    allergen_val = AllergenValidator(plan_input.catalog)
    diet_val = DietaryRestrictionValidator(plan_input.catalog)
    soft_penalty: dict[str, int] = {}
    valid_recipes: list[CandidateRecipeDTO] = []
    seen_warn: set[str] = set()

    for recipe in plan_input.candidates:
        allergen = allergen_val.validate(recipe, plan_input.members)
        for w in allergen.warnings:
            if w not in seen_warn:
                warnings.append(w)
                seen_warn.add(w)
        diet = diet_val.validate(recipe, plan_input.members)
        equipment_ok = recipe.required_equipment <= plan_input.available_equipment
        if allergen.valid and diet.valid and equipment_ok:
            valid_recipes.append(recipe)
            soft_penalty[recipe.recipe_id] = len(diet.soft_violations)

    # Feasible candidates per slot (by meal type + max prep time). Rejected
    # recipes stay in but are heavily penalized by the optimizer.
    feasible: dict[int, list[CandidateRecipeDTO]] = {}
    for slot in slots:
        feasible[slot.index] = [
            r
            for r in valid_recipes
            if slot.meal_type in r.meal_types
            and (
                slot.maximum_preparation_minutes is None
                or r.preparation_minutes + r.cooking_minutes
                <= slot.maximum_preparation_minutes
            )
        ]

    explainer = ConstraintExplainer()

    if not slots:
        return _empty_plan(plan_input)

    rejected_ids: set[str] = set()
    for member in plan_input.members:
        rejected_ids |= member.rejected_recipe_ids

    optimizer = PlanOptimizer(
        provisioner=provisioner,
        weights=plan_input.weights,
        budget=plan_input.budget,
        favorites=plan_input.favorites,
        rejected_recipe_ids=rejected_ids,
        soft_penalty=soft_penalty,
        seed=plan_input.seed,
    )
    outcome = optimizer.optimize(slots, feasible)

    # --- no feasible assignment: a required meal type has no candidate ------ #
    if not outcome.feasible:
        hard_tokens = _active_hard_tokens(plan_input)
        conflict, actions = explainer.missing_candidate_conflict(
            outcome.missing_meal_types, hard_tokens
        )
        return InfeasibleResult(
            minimal_conflict=conflict,
            suggested_actions=actions,
            relaxable_soft_constraints=_soft_tokens(plan_input),
            warnings=warnings,
            seed=plan_input.seed,
        )

    # --- over budget: diagnose instead of returning a fake plan ------------- #
    if outcome.over_budget:
        cheapest_cost = outcome.cheapest_cost or Decimal("0")
        cheapest_prov = outcome.cheapest_provision or outcome.provision
        offenders = (
            explainer.offending_products(cheapest_prov, plan_input.budget.amount)
            if cheapest_prov
            else []
        )
        conflict, relaxable, actions = explainer.budget_conflict(
            plan_input.budget,
            cheapest_cost,
            len(slots),
            _soft_tokens(plan_input),
            plan_input.weights,
        )
        return InfeasibleResult(
            minimal_conflict=conflict,
            min_budget_found=cheapest_cost,
            offending_products=offenders,
            relaxable_soft_constraints=relaxable,
            suggested_actions=actions,
            warnings=warnings,
            seed=plan_input.seed,
        )

    return _build_result(plan_input, slots, outcome, provisioner, matcher, converter, warnings)


def _empty_plan(plan_input: PlanInput) -> PlanResult:
    return PlanResult(
        planned_meals=[],
        grocery_lines=[],
        cost_per_day={},
        cost_total=CostTotal(known=Decimal("0"), estimated=Decimal("0"), total=Decimal("0")),
        budget_diff=plan_input.budget.amount,
        leftovers=[],
        pantry_used=[],
        coverage=compute_coverage([]),
        warnings=["no meals requested"],
        explanations=[],
        seed=plan_input.seed,
    )


def _build_result(
    plan_input: PlanInput,
    slots: list[MealSlot],
    outcome,  # OptimizerOutcome
    provisioner: Provisioner,
    matcher: ProductMatcher,
    converter: UnitConverter,
    warnings: list[str],
) -> PlanResult:
    assert outcome.provision is not None
    prov = outcome.provision
    participants = [m.alias for m in plan_input.members]

    meals: list[MealAssignment] = [
        MealAssignment(
            slot_index=slot.index,
            date=slot.date,
            meal_type=slot.meal_type,
            recipe=recipe,
            servings=slot.servings,
            participants=tuple(participants),
        )
        for slot, recipe in zip(slots, outcome.assignment, strict=True)
    ]

    # Marginal cost = incremental cost of adding each meal, in scheduled order.
    nutrition_calc = NutritionCalculator(matcher, converter)
    explainer_meals: list[str] = []
    planned: list[PlannedMealDTO] = []
    cost_per_day: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    prev_total = Decimal("0")

    for i, meal in enumerate(meals):
        step_prov = provisioner.provision(meals[: i + 1])
        marginal = step_prov.cost_total - prev_total
        prev_total = step_prov.cost_total

        imputable = prov.imputable_by_meal.get(meal.slot_index, Decimal("0"))
        nutrition, complete = nutrition_calc.for_meal(meal)
        explanation = explain_meal(meal, plan_input.favorites, plan_input.budget, imputable)
        explainer_meals.append(explanation)

        planned.append(
            PlannedMealDTO(
                recipe_id=meal.recipe.recipe_id,
                title=meal.recipe.title,
                date=meal.date,
                meal_type=cast(MealType, meal.meal_type),
                servings=meal.servings,
                participants=participants,
                cost=CostBreakdown(
                    total=imputable, imputable=imputable, marginal=marginal
                ),
                nutrition=nutrition,
                nutrition_complete=complete,
                explanation=explanation,
            )
        )
        cost_per_day[meal.date.isoformat()] += imputable

    coverage = compute_coverage(prov.grocery_lines)
    if coverage.status == "stale":
        warnings.append("plan uses expired prices; refresh the catalog")
    if coverage.status in ("insufficient", "none"):
        warnings.append("price coverage is low; total cost is not reliable")

    all_warnings = warnings + prov.warnings
    cost_total = CostTotal(
        known=prov.cost_known,
        estimated=prov.cost_estimated,
        total=prov.cost_total,
    )

    return PlanResult(
        planned_meals=planned,
        grocery_lines=prov.grocery_lines,
        cost_per_day=dict(sorted(cost_per_day.items())),
        cost_total=cost_total,
        budget_diff=plan_input.budget.amount - prov.cost_total,
        leftovers=prov.leftovers,
        pantry_used=prov.pantry_used,
        coverage=coverage,
        warnings=all_warnings,
        explanations=explainer_meals,
        seed=plan_input.seed,
    )


def _soft_tokens(plan_input: PlanInput) -> list[str]:
    tokens: set[str] = set()
    for member in plan_input.members:
        tokens.update(member.soft_preferences)
    return sorted(tokens)


def _active_hard_tokens(plan_input: PlanInput) -> list[str]:
    tokens: set[str] = set()
    for member in plan_input.members:
        tokens |= {a.lower() for a in member.allergens}
        tokens |= {r.lower() for r in member.hard_restrictions}
    return sorted(tokens)
