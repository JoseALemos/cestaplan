"""Auditable explanations (OPTIMIZATION.md §2.12, §7).

Explains why each meal was chosen, and — when no plan fits — the minimal set of
conflicting constraints, the minimum budget found, the products driving the
excess, the relaxable soft constraints, and the actions offered to the user.
Never fabricates a solution.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from cestaplan_engine.contracts import (
    BudgetDTO,
    OffendingProduct,
    ScoringWeights,
)
from cestaplan_engine.provisioning import MealAssignment, Provision


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def explain_meal(
    meal: MealAssignment,
    favorites: set[str],
    budget: BudgetDTO,
    imputable: Decimal,
) -> str:
    """Human-readable, auditable reason a recipe was placed on its slot."""
    recipe = meal.recipe
    reasons: list[str] = []
    if recipe.recipe_id in favorites:
        reasons.append("marked favorite")
    total_minutes = recipe.preparation_minutes + recipe.cooking_minutes
    reasons.append(f"prep+cook {total_minutes} min")
    reasons.append(f"imputable cost {_money(imputable)} {budget.currency}")
    if recipe.leftover_reuse:
        reasons.append("reuses leftovers")
    return (
        f"{meal.meal_type} on {meal.date.isoformat()}: '{recipe.title}' "
        f"({'; '.join(reasons)})"
    )


class ConstraintExplainer:
    """Builds the infeasibility diagnosis and soft-constraint relaxations."""

    def offending_products(
        self, provision: Provision, budget: Decimal, limit: int = 5
    ) -> list[OffendingProduct]:
        priced = sorted(
            provision.grocery_lines,
            key=lambda line: line.subtotal,
            reverse=True,
        )
        offenders: list[OffendingProduct] = []
        for line in priced[:limit]:
            if line.subtotal <= 0:
                continue
            offenders.append(
                OffendingProduct(
                    canonical_name=line.canonical_name,
                    product_id=line.product_id,
                    display_name=line.display_name,
                    subtotal=line.subtotal,
                )
            )
        return offenders

    def budget_conflict(
        self,
        budget: BudgetDTO,
        min_budget_found: Decimal,
        n_meals: int,
        soft_active: list[str],
        weights: ScoringWeights,
    ) -> tuple[list[str], list[str], list[str]]:
        """Return ``(minimal_conflict, relaxable_soft, suggested_actions)``."""
        minimal_conflict = [
            f"budget:{_money(budget.amount)} {budget.currency}",
            f"meals:{n_meals}",
            "store_catalog_prices",
        ]
        relaxable = sorted(set(soft_active))
        actions = [
            f"raise_budget_to:{_money(min_budget_found)} {budget.currency}",
            "reduce_meals",
            "change_store",
            "accept_estimated_prices",
        ]
        return minimal_conflict, relaxable, actions

    def missing_candidate_conflict(
        self, meal_types: list[str], hard_tokens: list[str]
    ) -> tuple[list[str], list[str]]:
        """Conflict + actions when a required meal type has no feasible recipe."""
        conflict = [f"no_candidate_for:{mt}" for mt in meal_types]
        conflict += [f"hard_constraint:{t}" for t in sorted(set(hard_tokens))]
        actions = [
            "add_recipes",
            "relax_soft_preferences",
            "change_store",
            "reduce_meals",
        ]
        return conflict, actions
