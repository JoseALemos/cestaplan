"""Deterministic planner preflight — refuse to run the optimizer on an impossible precondition.

The optimizer only knows two failure modes (a meal slot has no candidate, or the cheapest plan is
over budget). With an EMPTY catalog it does neither: it returns a 0-cost "plan" with no coverage,
or a bare ``no_candidate_for`` conflict whose generic actions (``change_store``…) wrongly imply a
store/budget problem. This module runs BEFORE the solver, queries the productive catalog directly,
and returns a TYPED cause so the UI never blames the budget for what is really an empty catalogue.

Ordering is deliberate: recipe/catalog/price/costable causes are checked BEFORE any budget notion,
so ``genuine_budget_infeasibility`` (decided by the engine, never here) can only surface once a real
costable catalogue exists. Nothing here invents prices, recipes or products.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    IngredientProductMapping,
    MealPlan,
    ProductPrice,
    Recipe,
    RecipeIngredient,
    Retailer,
)


class PreflightCode(StrEnum):
    """Typed precondition causes. ``genuine_budget_infeasibility``/``optimizer_error`` come from the
    engine layer, not from this preflight, but share the vocabulary so the UI keys on one enum."""

    NO_ACTIVE_RECIPES = "no_active_recipes"
    NO_COMPATIBLE_RECIPES = "no_compatible_recipes"
    NO_RETAILER_SELECTED = "no_retailer_selected"
    RETAILER_WITHOUT_CATALOG = "retailer_without_catalog"
    NO_MAPPED_PRODUCTS = "no_mapped_products"
    NO_PRODUCT_PRICES = "no_product_prices"
    NO_COSTABLE_RECIPES = "no_costable_recipes"
    INSUFFICIENT_RECIPE_VARIETY = "insufficient_recipe_variety"
    GENUINE_BUDGET_INFEASIBILITY = "genuine_budget_infeasibility"
    HARD_CONSTRAINTS_INFEASIBLE = "hard_constraints_infeasible"
    OPTIMIZER_ERROR = "optimizer_error"


class ActionCode(StrEnum):
    """Recovery actions (typed; the UI translates them — never render the slug)."""

    ADD_RECIPES = "add_recipes"
    RELAX_SOFT_PREFERENCES = "relax_soft_preferences"
    CHANGE_STORE = "change_store"
    REDUCE_MEALS = "reduce_meals"
    INCREASE_BUDGET = "increase_budget"
    CONFIGURE_PROVIDER = "configure_provider"
    REVIEW_MAPPINGS = "review_mappings"


# Reason + actions per preflight code. Only genuine_budget_infeasibility recommends more budget.
_REASONS: dict[PreflightCode, tuple[str, list[ActionCode]]] = {
    PreflightCode.NO_ACTIVE_RECIPES: (
        "No hay recetas activas disponibles para construir el plan.",
        [ActionCode.ADD_RECIPES],
    ),
    PreflightCode.NO_COMPATIBLE_RECIPES: (
        "Ninguna receta es compatible con las restricciones del hogar.",
        [ActionCode.ADD_RECIPES, ActionCode.RELAX_SOFT_PREFERENCES],
    ),
    PreflightCode.NO_RETAILER_SELECTED: (
        "Selecciona una cadena con catálogo para poder calcular precios.",
        [ActionCode.CHANGE_STORE],
    ),
    PreflightCode.RETAILER_WITHOUT_CATALOG: (
        "La cadena seleccionada todavía no tiene un catálogo cargado.",
        [ActionCode.CHANGE_STORE, ActionCode.CONFIGURE_PROVIDER],
    ),
    PreflightCode.NO_MAPPED_PRODUCTS: (
        "Todavía no hay productos asociados a los ingredientes.",
        [ActionCode.REVIEW_MAPPINGS, ActionCode.CONFIGURE_PROVIDER],
    ),
    PreflightCode.NO_PRODUCT_PRICES: (
        "Todavía no hay precios disponibles para calcular un plan con presupuesto.",
        [ActionCode.CONFIGURE_PROVIDER],
    ),
    PreflightCode.NO_COSTABLE_RECIPES: (
        "Hay recetas disponibles, pero ninguna tiene todos sus ingredientes y precios necesarios "
        "para calcular el coste.",
        [ActionCode.REVIEW_MAPPINGS, ActionCode.ADD_RECIPES],
    ),
    PreflightCode.INSUFFICIENT_RECIPE_VARIETY: (
        "No hay suficiente variedad de recetas costeables para las comidas solicitadas.",
        [ActionCode.ADD_RECIPES, ActionCode.REDUCE_MEALS],
    ),
}


@dataclass(slots=True)
class PreflightOutcome:
    """Result of the deterministic preflight. ``ok`` means the solver may run."""

    ok: bool
    code: PreflightCode | None = None
    reason: str | None = None
    suggested_actions: list[ActionCode] = field(default_factory=list)
    candidate_counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)

    def to_report(self) -> dict[str, object]:
        """Shape consumed by the frontend under ``infeasibility``. Never invents budget/products."""
        return {
            "status": "infeasible",
            "code": self.code.value if self.code else None,
            "reason": self.reason,
            "details": self.details,
            "candidate_counts": self.candidate_counts,
            "suggested_actions": [a.value for a in self.suggested_actions],
            # Preflight never computes a minimum budget or offending products — those require a
            # real costed plan (the engine's budget path), which by definition did not run here.
            "minimum_budget": None,
            "offending_products": [],
            "minimal_conflict": [f"code:{self.code.value}"] if self.code else [],
            "warnings": [],
        }


def _fail(code: PreflightCode, counts: dict[str, int], **details: object) -> PreflightOutcome:
    reason, actions = _REASONS[code]
    return PreflightOutcome(
        ok=False,
        code=code,
        reason=reason,
        suggested_actions=actions,
        candidate_counts=counts,
        details={k: v for k, v in details.items() if v is not None},
    )


def evaluate(
    *,
    recipes_active: int,
    retailer_selected: bool,
    approved_mappings: int,
    productive_prices: int,
    costable_recipes: int,
    requested_meal_types: int,
    retailer_slug: str | None = None,
) -> PreflightOutcome:
    """Pure decision over already-gathered counts (order: recipes → retailer catalogue → mapped
    products → prices → costable → variety). Budget is NEVER evaluated here, so the budget message
    can never surface for an empty catalogue. Deterministic and unit-testable without a DB."""
    counts = {
        "recipes_active": recipes_active,
        "approved_mappings": approved_mappings,
        "productive_prices": productive_prices,
        "costable_recipes": costable_recipes,
        "requested_meal_types": requested_meal_types,
    }
    if recipes_active == 0:
        return _fail(PreflightCode.NO_ACTIVE_RECIPES, counts)
    if retailer_selected and productive_prices == 0:
        return _fail(PreflightCode.RETAILER_WITHOUT_CATALOG, counts, retailer=retailer_slug)
    if approved_mappings == 0:
        return _fail(PreflightCode.NO_MAPPED_PRODUCTS, counts)
    if productive_prices == 0:
        return _fail(PreflightCode.NO_PRODUCT_PRICES, counts)
    if costable_recipes == 0:
        return _fail(PreflightCode.NO_COSTABLE_RECIPES, counts)
    if costable_recipes < max(1, requested_meal_types):
        return _fail(
            PreflightCode.INSUFFICIENT_RECIPE_VARIETY,
            counts,
            requested_meal_types=requested_meal_types,
        )
    return PreflightOutcome(ok=True, candidate_counts=counts)


def run_preflight(db: Session, meal_plan: MealPlan) -> PreflightOutcome:
    """Gather productive-catalog counts (read-only) and evaluate the deterministic preflight."""
    retailer_id = meal_plan.retailer_id
    price_where = [ProductPrice.retailer_id == retailer_id] if retailer_id is not None else []

    recipes_active = int(
        db.scalar(select(func.count()).select_from(Recipe).where(Recipe.is_public.is_(True))) or 0
    )
    approved_mappings = int(
        db.scalar(
            select(func.count())
            .select_from(IngredientProductMapping)
            .where(IngredientProductMapping.is_active.is_(True))
        )
        or 0
    )
    productive_prices = int(
        db.scalar(select(func.count()).select_from(ProductPrice).where(*price_where)) or 0
    )
    # Only compute the (heavier) costable set once the cheap preconditions could pass.
    costable = (
        _count_costable_recipes(db, retailer_id)
        if recipes_active and approved_mappings and productive_prices
        else 0
    )
    retailer = db.get(Retailer, retailer_id) if retailer_id is not None else None
    return evaluate(
        recipes_active=recipes_active,
        retailer_selected=retailer_id is not None,
        approved_mappings=approved_mappings,
        productive_prices=productive_prices,
        costable_recipes=costable,
        requested_meal_types=_requested_meal_type_count(db, meal_plan),
        retailer_slug=getattr(retailer, "slug", None),
    )


def _count_costable_recipes(db: Session, retailer_id: int | None) -> int:
    """A public recipe is costable when every MANDATORY ingredient maps (active) to a product that
    has at least one price (retailer-scoped when a retailer is selected)."""
    price_where = [ProductPrice.retailer_id == retailer_id] if retailer_id is not None else []
    priced_product_ids = {
        row[0]
        for row in db.execute(select(ProductPrice.product_id).where(*price_where).distinct()).all()
    }
    if not priced_product_ids:
        return 0
    mapped_ingredient_ids = {
        row[0]
        for row in db.execute(
            select(IngredientProductMapping.ingredient_id)
            .where(
                IngredientProductMapping.is_active.is_(True),
                IngredientProductMapping.product_id.in_(priced_product_ids),
            )
            .distinct()
        ).all()
    }
    if not mapped_ingredient_ids:
        return 0

    costable = 0
    recipe_ids = [
        row[0]
        for row in db.execute(select(Recipe.id).where(Recipe.is_public.is_(True))).all()
    ]
    for recipe_id in recipe_ids:
        mandatory = [
            row[0]
            for row in db.execute(
                select(RecipeIngredient.ingredient_id).where(
                    RecipeIngredient.recipe_id == recipe_id,
                    RecipeIngredient.optional.is_(False),
                )
            ).all()
        ]
        if mandatory and all(iid in mapped_ingredient_ids for iid in mandatory):
            costable += 1
    return costable


def _requested_meal_type_count(db: Session, meal_plan: MealPlan) -> int:
    from cestaplan_api.models import MealRequirement

    rows = db.execute(
        select(MealRequirement.requested_count).where(
            MealRequirement.meal_plan_id == meal_plan.id,
            MealRequirement.requested_count > 0,
        )
    ).all()
    return len(rows)


__all__ = ["ActionCode", "PreflightCode", "PreflightOutcome", "run_preflight"]
