"""Plan optimizer: greedy + bounded backtracking, reproducible via seed (§4, §5).

Chooses the best recipe for each slot under the configurable weighted score and
the budget. Deterministic: with the same input and seed it returns the same plan.
Also records the globally cheapest plan it saw so the caller can explain an
infeasible (over-budget) outcome.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from cestaplan_engine.contracts import (
    BudgetDTO,
    CandidateRecipeDTO,
    ScoringWeights,
)
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
    ) -> None:
        self._prov = provisioner
        self._w = weights
        self._budget = budget
        self._favorites = favorites
        self._rejected = rejected_recipe_ids
        self._soft = soft_penalty
        self._rng = random.Random(seed)
        self._max_passes = max_passes
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

    def _score(
        self, recipes: list[CandidateRecipeDTO], prov: Provision
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

        distinct = len({r.recipe_id for r in recipes})
        repetition = Decimal(len(recipes) - distinct)
        time_minutes = Decimal(
            sum(r.preparation_minutes + r.cooking_minutes for r in recipes)
        ) / Decimal("60")
        favorite_bonus = Decimal(sum(1 for r in recipes if r.recipe_id in self._favorites))
        rejected = Decimal(sum(1 for r in recipes if r.recipe_id in self._rejected))
        soft = Decimal(sum(self._soft.get(r.recipe_id, 0) for r in recipes))

        score = (
            w.cost * cost_total
            + w.waste * waste_value
            + w.repetition * repetition
            + w.time * time_minutes
            + w.soft * soft
            - w.pantry * Decimal(pantry_bonus)
            - w.favorite * favorite_bonus
            + w.rejected * rejected
        )
        if self._budget.strict and cost_total > self._budget_limit():
            score += _BUDGET_PENALTY * (cost_total - self._budget_limit())
        return score

    def _evaluate(
        self, slots: list[MealSlot], recipes: list[CandidateRecipeDTO]
    ) -> tuple[Decimal, Provision]:
        prov = self._prov.provision(self._build_meals(slots, recipes))
        self._track_cheapest(recipes, prov)
        return self._score(recipes, prov), prov

    def _track_cheapest(
        self, recipes: list[CandidateRecipeDTO], prov: Provision
    ) -> None:
        cost = prov.cost_total
        if self._cheapest_cost is None or cost < self._cheapest_cost:
            self._cheapest_cost = cost
            self._cheapest_assignment = list(recipes)
            self._cheapest_provision = prov

    # -- search ----------------------------------------------------------- #
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

        # Greedy: fill slots left-to-right choosing the recipe that minimizes the
        # score of the partial plan (accounts for leftover reuse via provisioning).
        chosen: list[CandidateRecipeDTO] = []
        for i, slot in enumerate(slots):
            options = self._ordered(feasible[slot.index])
            best_recipe: CandidateRecipeDTO | None = None
            best_score = None
            for recipe in options:
                trial = [*chosen, recipe]
                score, _ = self._evaluate(slots[: i + 1], trial)
                key = (score, recipe.recipe_id)
                if best_score is None or key < best_score:
                    best_score = key
                    best_recipe = recipe
            assert best_recipe is not None
            chosen.append(best_recipe)

        best_score, best_prov = self._evaluate(slots, chosen)

        # Bounded backtracking: try single-slot swaps until no improvement.
        for _ in range(self._max_passes):
            improved = False
            for i, slot in enumerate(slots):
                current = chosen[i]
                for recipe in self._ordered(feasible[slot.index]):
                    if recipe.recipe_id == current.recipe_id:
                        continue
                    trial = list(chosen)
                    trial[i] = recipe
                    score, prov = self._evaluate(slots, trial)
                    if (score, recipe.recipe_id) < (best_score, current.recipe_id):
                        chosen = trial
                        best_score = score
                        best_prov = prov
                        improved = True
                        break
            if not improved:
                break

        over_budget = best_prov.cost_total > self._budget_limit()
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
