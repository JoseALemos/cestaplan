"""CestaPlan deterministic engine.

Independent of the web layer and of OpenAI. It validates and computes everything
critical: hard constraints, unit conversion, full-package selection, cost and
nutrition. OpenAI only proposes candidates; this engine decides (see ADR-0004).

Public API::

    from cestaplan_engine import generate_plan, PlanInput
    result = generate_plan(plan_input)  # PlanResult | InfeasibleResult
"""

from __future__ import annotations

from cestaplan_engine.contracts import (
    BudgetDTO,
    CandidateRecipeDTO,
    CatalogProductDTO,
    CostBreakdown,
    CostTotal,
    CoverageDTO,
    GroceryLineDTO,
    InfeasibleResult,
    IngredientConversionDTO,
    LeftoverDTO,
    MealRequirementDTO,
    MemberDTO,
    NutritionDTO,
    PackageOptionDTO,
    PantryItemDTO,
    PlanInput,
    PlannedMealDTO,
    PlanResult,
    RecipeIngredientDTO,
    ScoringWeights,
)
from cestaplan_engine.facade import generate_plan

__version__ = "0.0.0"

__all__ = [
    "BudgetDTO",
    "CandidateRecipeDTO",
    "CatalogProductDTO",
    "CostBreakdown",
    "CostTotal",
    "CoverageDTO",
    "GroceryLineDTO",
    "InfeasibleResult",
    "IngredientConversionDTO",
    "LeftoverDTO",
    "MealRequirementDTO",
    "MemberDTO",
    "NutritionDTO",
    "PackageOptionDTO",
    "PantryItemDTO",
    "PlanInput",
    "PlanResult",
    "PlannedMealDTO",
    "RecipeIngredientDTO",
    "ScoringWeights",
    "__version__",
    "generate_plan",
]
