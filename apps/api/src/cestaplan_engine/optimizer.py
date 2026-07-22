"""Plan optimizer: greedy + bounded backtracking, reproducible via seed (§4, §5).

Chooses the best recipe for each slot under the configurable weighted score and
the budget. Deterministic: with the same input and seed it returns the same plan.
Also records the globally cheapest plan it saw so the caller can explain an
infeasible (over-budget) outcome.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from cestaplan_engine.contracts import (
    BudgetDTO,
    CandidateRecipeDTO,
    NutritionTargetDTO,
    ScoringWeights,
)
from cestaplan_engine.nutrition import _MACROS, NutritionCalculator
from cestaplan_engine.provisioning import MealAssignment, Provision, Provisioner
from cestaplan_engine.scheduling import MealSlot

_BUDGET_PENALTY = Decimal("1000000")  # steers strict search toward feasibility


@dataclass
class OptimizerOutcome:
    feasible: bool
    missing_meal_types: list[str] = field(default_factory=list)
    assignment: list[CandidateRecipeDTO] = field(default_factory=list)
    provision: Provision | None = None
    score: Decimal = Decimal("0")
    over_budget: bool = False
    cheapest_cost: Decimal | None = None
    cheapest_assignment: list[CandidateRecipeDTO] = field(default_factory=list)
    cheapest_provision: Provision | None = None


class PlanOptimizer:
    def __init__(
        self,
        provisioner: Provisioner,
        weights: ScoringWeights,
        budget: BudgetDTO,
        favorites: set[str],
        rejected_recipe_ids: set[str],
        soft_penalty: dict[str, int],
        seed: int = 0,
        max_passes: int = 4,
        nutrition_target: NutritionTargetDTO | None = None,
        nutrition_calc: NutritionCalculator | None = None,
        num_days: int = 0,
    ) -> None:
        self._prov = provisioner
        self._w = weights
        self._budget = budget
        self._favorites = favorites
        self._rejected = rejected_recipe_ids
        self._soft = soft_penalty
        self._rng = random.Random(seed)
        self._max_passes = max_passes
        # Nutrition fitting is inert unless a target is set: with ``nutrition_target``
        # None the extra term is 0 and the search is byte-identical to before.
        self._nutrition_target = nutrition_target
        self._nutrition_calc = nutrition_calc
        self._num_days = num_days if num_days > 0 else 1
        self._cheapest_cost: Decimal | None = None
        self._cheapest_assignment: list[CandidateRecipeDTO] = []
        self._cheapest_provision: Provision | None = None

    # -- scoring ---------------------------------------------------------- #
    def _budget_limit(self) -> Decimal:
        limit = self._budget.amount
        if not self._budget.strict:
            limit = limit * (Decimal("1") + self._budget.max_margin_ratio)
        return limit

    def _build_meals(
        self, slots: list[MealSlot], recipes: list[CandidateRecipeDTO]
    ) -> list[MealAssignment]:
        meals: list[MealAssignment] = []
        for slot, recipe in zip(slots, recipes, strict=True):
            meals.append(
                MealAssignment(
                    slot_index=slot.index,
                    date=slot.date,
                    meal_type=slot.meal_type,
                    recipe=recipe,
                    servings=slot.servings,
                    participants=(),
                )
            )
        return meals

    def _nutrition_penalty(
        self, slots: list[MealSlot], recipes: list[CandidateRecipeDTO]
    ) -> Decimal:
        """Relative per-macro distance of the plan's per-day nutrition from target.

        Sums, over each targeted macro, ``|actual_per_day - target| / target`` so the
        term is scale-free across macros. Only macros with a positive target count.
        Returns 0 when there is no target (keeps the no-target path unchanged).
        """
        target = self._nutrition_target
        calc = self._nutrition_calc
        if target is None or calc is None:
            return Decimal("0")
        totals, _complete = calc.for_meals(self._build_meals(slots, recipes))
        days = Decimal(self._num_days)
        penalty = Decimal("0")
        for macro in _MACROS:
            goal = getattr(target, macro)
            if goal is None or goal <= 0:
                continue
            actual_per_day = totals[macro] / days
            penalty += abs(actual_per_day - goal) / goal
        return penalty

    def _score(
        self,
        recipes: list[CandidateRecipeDTO],
        prov: Provision,
        slots: list[MealSlot],
    ) -> Decimal:
        w = self._w
        cost_total = prov.cost_total

        waste_value = Decimal("0")
        pantry_bonus = 0
        for line in prov.grocery_lines:
            if line.purchased_quantity > 0 and line.subtotal > 0:
                waste_value += line.subtotal * line.leftover / line.purchased_quantity
            if line.pantry_quantity > 0:
                pantry_bonus += 1

        # Variety (anti-repetition): penalize each *reuse* of a recipe
        # SUPERLINEARLY. For a recipe used k times the penalty is the triangular
        # number k*(k-1)/2 (the j-th reuse costs j), so the marginal cost of the
        # 2nd use is 1, the 3rd is 2, the 4th is 3, ... This makes a 4x-repeated
        # dish (penalty 6) far worse than spreading over 4 distinct recipes
        # (penalty 0). Because a recipe carries fixed meal_types, reuse is
        # inherently within meal type, so this also enforces intra-type variety.
        # With the retuned w.repetition (see contracts.py) one reuse dominates the
        # small waste/time/preference differences between similar recipes, so a
        # rich candidate pool yields close to the maximum number of distinct dishes.
        counts = Counter(r.recipe_id for r in recipes)
        repetition = Decimal(sum(c * (c - 1) // 2 for c in counts.values()))
        time_minutes = Decimal(
            sum(r.preparation_minutes + r.cooking_minutes for r in recipes)
        ) / Decimal("60")
        favorite_bonus = Decimal(sum(1 for r in recipes if r.recipe_id in self._favorites))
        rejected = Decimal(sum(1 for r in recipes if r.recipe_id in self._rejected))
        soft = Decimal(sum(self._soft.get(r.recipe_id, 0) for r in recipes))

        score = (
            w.waste * waste_value
            + w.repetition * repetition
            + w.time * time_minutes
            + w.soft * soft
            - w.pantry * Decimal(pantry_bonus)
            - w.favorite * favorite_bonus
            + w.rejected * rejected
        )
        # Nutrition fitting: a small penalty for the plan's per-day macros drifting
        # from the household target. The weight (1.2) is far below the variety driver
        # (12) and the budget cap (1e6), so it only breaks ties toward the target and
        # never overrides budget/allergen/variety guarantees. Exactly 0 (no-op) when
        # no target is set, so the no-target plan is unchanged.
        score += w.nutrition_deviation * self._nutrition_penalty(slots, recipes)
        # Cost as an OBJECTIVE only when the household asked for the cheapest plan
        # (budget.priority == "price"). Under the default "waste" priority the
        # budget is an ENVELOPE, not something to minimize: within it we optimize
        # for variety + preferences + low waste + low time, so plans no longer
        # collapse onto the single cheapest dish repeated.
        if self._budget.priority == "price":
            score += w.cost * cost_total
        # Budget CAP (both priorities): cost above the limit — amount when strict,
        # amount*(1+max_margin_ratio) when flexible — is penalized so heavily that
        # the search always steers under budget when any feasible plan exists, and
        # only reports over-budget (-> InfeasibleResult) when none does.
        limit = self._budget_limit()
        if cost_total > limit:
            score += _BUDGET_PENALTY * (cost_total - limit)
        return score

    def _provision(
        self, slots: list[MealSlot], recipes: list[CandidateRecipeDTO]
    ) -> Provision:
        return self._prov.provision(self._build_meals(slots, recipes))

    def _track_cheapest(
        self, recipes: list[CandidateRecipeDTO], prov: Provision
    ) -> None:
        # Only whole plans are tracked (the caller passes full assignments): a
        # partial plan is always cheaper than the full one, so tracking partials
        # would make ``cheapest_cost`` a meaningless under-count that could report
        # a sub-budget "min" for an over-budget plan.
        cost = prov.cost_total
        if self._cheapest_cost is None or cost < self._cheapest_cost:
            self._cheapest_cost = cost
            self._cheapest_assignment = list(recipes)
            self._cheapest_provision = prov

    # -- search ----------------------------------------------------------- #
    def _greedy(
        self,
        slots: list[MealSlot],
        feasible: dict[int, list[CandidateRecipeDTO]],
        *,
        cost_only: bool,
    ) -> list[CandidateRecipeDTO]:
        """Fill slots left-to-right. ``cost_only`` seeds a CHEAP plan (minimize
        partial cost); otherwise seeds a VARIETY-optimal plan (minimize the full
        score of the partial plan)."""
        chosen: list[CandidateRecipeDTO] = []
        for i, slot in enumerate(slots):
            best_recipe: CandidateRecipeDTO | None = None
            best_key: tuple[Decimal, str] | None = None
            for recipe in self._ordered(feasible[slot.index]):
                trial = [*chosen, recipe]
                prov = self._provision(slots[: i + 1], trial)
                metric = (
                    prov.cost_total
                    if cost_only
                    else self._score(trial, prov, slots[: i + 1])
                )
                key = (metric, recipe.recipe_id)
                if best_key is None or key < best_key:
                    best_key = key
                    best_recipe = recipe
            assert best_recipe is not None
            chosen.append(best_recipe)
        return chosen

    def _local_search(
        self,
        slots: list[MealSlot],
        feasible: dict[int, list[CandidateRecipeDTO]],
        chosen: list[CandidateRecipeDTO],
    ) -> tuple[list[CandidateRecipeDTO], Decimal, Provision]:
        """Bounded single-slot-swap backtracking from ``chosen`` until no swap
        improves the score. The score's huge over-budget penalty means that while
        the plan is over budget the search drives cost down; once under budget it
        improves variety/preferences without crossing back over the cap."""
        prov = self._provision(slots, chosen)
        self._track_cheapest(chosen, prov)
        best_score = self._score(chosen, prov, slots)
        best_prov = prov
        for _ in range(self._max_passes):
            improved = False
            for i, slot in enumerate(slots):
                current = chosen[i]
                for recipe in self._ordered(feasible[slot.index]):
                    if recipe.recipe_id == current.recipe_id:
                        continue
                    trial = list(chosen)
                    trial[i] = recipe
                    tprov = self._provision(slots, trial)
                    self._track_cheapest(trial, tprov)
                    tscore = self._score(trial, tprov, slots)
                    if (tscore, recipe.recipe_id) < (best_score, current.recipe_id):
                        chosen = trial
                        best_score = tscore
                        best_prov = tprov
                        improved = True
                        break
            if not improved:
                break
        return chosen, best_score, best_prov

    def optimize(
        self,
        slots: list[MealSlot],
        feasible: dict[int, list[CandidateRecipeDTO]],
    ) -> OptimizerOutcome:
        missing = [
            slots[i].meal_type
            for i in range(len(slots))
            if not feasible.get(slots[i].index)
        ]
        if missing:
            return OptimizerOutcome(feasible=False, missing_meal_types=sorted(set(missing)))

        limit = self._budget_limit()

        # Pass 1: variety-optimal search (unconstrained). For a comfortable budget
        # this already lands within the cap and yields the richest plan.
        chosen, best_score, best_prov = self._local_search(
            slots, feasible, self._greedy(slots, feasible, cost_only=False)
        )

        over_budget = best_prov.cost_total > limit
        if over_budget:
            # The variety-optimal plan overshoots the budget. Because the budget is
            # a HARD constraint, re-run the search SEEDED from the cheapest plan
            # (cost-minimizing greedy). Its low starting cost lets backtracking keep
            # every accepted plan within the cap while it recovers as much variety
            # as fits. If this finds a within-budget plan we use it — a feasible
            # plan is never reported infeasible. Only if even this stays over the
            # cap is the request genuinely infeasible (cheapest_cost > limit then).
            fit_chosen, fit_score, fit_prov = self._local_search(
                slots, feasible, self._greedy(slots, feasible, cost_only=True)
            )
            if fit_prov.cost_total <= limit:
                chosen, best_score, best_prov = fit_chosen, fit_score, fit_prov
                over_budget = False

        return OptimizerOutcome(
            feasible=True,
            assignment=chosen,
            provision=best_prov,
            score=best_score,
            over_budget=over_budget,
            cheapest_cost=self._cheapest_cost,
            cheapest_assignment=self._cheapest_assignment,
            cheapest_provision=self._cheapest_provision,
        )

    def _ordered(
        self, options: list[CandidateRecipeDTO]
    ) -> list[CandidateRecipeDTO]:
        # Deterministic exploration order: sort by id, then a seeded shuffle. Final
        # selection is by score so the result is stable regardless of order.
        ordered = sorted(options, key=lambda r: r.recipe_id)
        self._rng.shuffle(ordered)
        return ordered
