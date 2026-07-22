"""Public Pydantic v2 DTOs — the deterministic engine's stable interface.

The service layer builds these DTOs from database rows and calls
``generate_plan``. The engine never imports SQLAlchemy, FastAPI or OpenAI: it
speaks only in these plain-data contracts.

Money and physical quantities are always :class:`~decimal.Decimal` (never
``float``) and serialize to JSON as strings so no precision is lost when the
result crosses the language boundary to TypeScript.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

# --- Decimal that serializes as a string in JSON mode, stays Decimal in Python.
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: str(v), return_type=str, when_used="json"),
]

MealType = Literal["breakfast", "lunch", "snack", "dinner"]
CoverageStatus = Literal["complete", "high", "partial", "insufficient", "stale", "none"]
Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]

WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class _Base(BaseModel):
    """Shared config: forbid unknown keys so malformed input fails loudly."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Input DTOs
# --------------------------------------------------------------------------- #
class MemberDTO(_Base):
    """A single household member and their per-person constraints."""

    alias: str
    relative_serving: DecimalStr = Decimal("1")
    allergens: set[str] = Field(default_factory=set)
    hard_restrictions: set[str] = Field(default_factory=set)
    soft_preferences: list[str] = Field(default_factory=list)
    rejected_recipe_ids: set[str] = Field(default_factory=set)


class MealRequirementDTO(_Base):
    """How many meals of a given type the household wants, and their shape."""

    meal_type: MealType
    requested_count: int = Field(ge=0)
    default_servings: int = Field(ge=1, default=1)
    selected_dates: list[date] | None = None
    auto_distribute: bool = True
    preferred_days: list[Weekday] | None = None
    maximum_preparation_minutes: int | None = None
    requires_tupper: bool = False
    reheating_available: bool = True

    @field_validator("maximum_preparation_minutes")
    @classmethod
    def _zero_means_no_limit(cls, value: int | None) -> int | None:
        # A max prep time of 0 (or negative) is not a real constraint — it would
        # silently reject every recipe. Treat it as "no limit" (None).
        return value if value and value > 0 else None


class BudgetDTO(_Base):
    """The spending constraint. ``strict`` makes it hard; otherwise a margin is allowed."""

    amount: DecimalStr
    currency: str = "EUR"
    strict: bool = True
    max_margin_ratio: DecimalStr = Decimal("0")
    includes_pantry_staples: bool = False
    includes_condiments: bool = False
    # "waste" (default): budget is an ENVELOPE — within it the optimizer maximizes
    # variety + preference fit + low waste, and does NOT drive cost to the minimum.
    # "price": the household wants the cheapest plan, so cost re-enters the score as
    # an active minimization term (see optimizer._score).
    priority: Literal["price", "waste"] = "waste"


class PackageOptionDTO(_Base):
    """One purchasable package format of a product (a store SKU + price snapshot)."""

    product_id: str
    package_quantity: DecimalStr
    package_unit: str
    amount: DecimalStr  # price of one whole package
    unit_price: DecimalStr = Decimal("0")
    availability: Literal["in_stock", "out_of_stock", "unknown"] = "unknown"
    source_type: str = "estimated"
    source_name: str = ""
    observed_at: date | None = None
    expires_at: date | None = None
    confidence_score: DecimalStr = Decimal("0")
    has_price: bool = True


class NutritionDTO(_Base):
    """Nutrition per 100 g of product (macros optional; ``None`` = not declared)."""

    kcal: DecimalStr | None = None
    protein_g: DecimalStr | None = None
    carbs_g: DecimalStr | None = None
    fat_g: DecimalStr | None = None


class NutritionTargetDTO(_Base):
    """Household-level per-day nutrition target the optimizer fits toward.

    Aggregated by the service layer from the members' ``DietaryProfile`` goals
    (each scaled by ``relative_serving``). Any subset of macros may be set; a
    ``None`` macro is simply not scored. A fully-empty target should be passed as
    ``None`` on :class:`PlanInput` (no target -> the nutrition term stays 0 and the
    plan is byte-identical to a run without this feature).
    """

    kcal: DecimalStr | None = None
    protein_g: DecimalStr | None = None
    carbs_g: DecimalStr | None = None
    fat_g: DecimalStr | None = None

    def is_empty(self) -> bool:
        return all(
            getattr(self, m) is None
            for m in ("kcal", "protein_g", "carbs_g", "fat_g")
        )


class CatalogProductDTO(_Base):
    """A store product linked to a canonical ingredient, with its packages."""

    product_id: str
    canonical_name: str
    display_name: str
    category: str = "uncategorized"
    packages: list[PackageOptionDTO] = Field(default_factory=list)
    nutrition: NutritionDTO | None = None  # per 100 g
    allergens: set[str] = Field(default_factory=set)


class RecipeIngredientDTO(_Base):
    """One ingredient line of a candidate recipe."""

    canonical_name: str
    display_name: str
    quantity: DecimalStr
    unit: str
    optional: bool = False
    substitution_group: str | None = None


class CandidateRecipeDTO(_Base):
    """A recipe proposed for the plan (from OpenAI or the seed library)."""

    recipe_id: str
    title: str
    description: str = ""
    servings: int = Field(ge=1, default=1)
    meal_types: set[MealType] = Field(default_factory=set)
    cuisine: str = ""
    preference_tags: list[str] = Field(default_factory=list)
    ingredients: list[RecipeIngredientDTO] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    preparation_minutes: int = 0
    cooking_minutes: int = 0
    required_equipment: set[str] = Field(default_factory=set)
    leftover_reuse: bool = False
    storage_instructions: str | None = None
    reheating_instructions: str | None = None
    allergens_declared: set[str] = Field(default_factory=set)


class PantryItemDTO(_Base):
    """Something already at home that reduces what must be bought."""

    canonical_name: str
    quantity: DecimalStr
    unit: str
    expires_at: date | None = None


class IngredientConversionDTO(_Base):
    """A conversion factor for a canonical ingredient.

    ``quantity[from_unit] * factor == quantity[to_unit]``. For mass<->volume the
    factor encodes the ingredient density (e.g. ml -> g).
    """

    canonical_name: str
    from_unit: str
    to_unit: str
    factor: DecimalStr


class ScoringWeights(_Base):
    """Configurable weights for the plan score. Lower score is better.

    Roles after the FASE 3 variety/budget retune (see optimizer._score):
    - ``waste``: euro-valued leftover penalty — avoid opening packs used little.
    - ``repetition``: the VARIETY driver. Multiplies a superlinear per-recipe reuse
      penalty (triangular in reuse count). Raised from 0.6 to 12 so that a single
      reuse (+12) dominates the small waste/time/preference gaps between similar
      recipes; a rich pool therefore yields near-maximum distinct dishes and no
      dish repeats more than a couple of times.
    - ``cost``: ONLY active when ``budget.priority == "price"`` (cheapest plan).
      Under the default "waste" priority the budget is an envelope, not an
      objective, so this term is not applied.
    - ``time``: prefer faster recipes.
    - ``nutrition_deviation``: reserved for nutritional-target fitting.
    - ``soft``: unmet soft dietary preferences.
    - ``pantry`` / ``favorite``: bonuses (subtracted) for using pantry items and
      favorite recipes.
    - ``rejected``: effectively a hard block (1e9) per rejected dish included.
    """

    waste: DecimalStr = Decimal("1.0")
    repetition: DecimalStr = Decimal("12")
    cost: DecimalStr = Decimal("1.5")
    time: DecimalStr = Decimal("0.8")
    nutrition_deviation: DecimalStr = Decimal("1.2")
    soft: DecimalStr = Decimal("1.0")
    pantry: DecimalStr = Decimal("1.0")
    favorite: DecimalStr = Decimal("0.7")
    rejected: DecimalStr = Decimal("1000000000")  # 1e9, effectively a hard block


class PlanInput(_Base):
    """Everything the engine needs to produce a plan, deterministically."""

    members: list[MemberDTO]
    meal_requirements: list[MealRequirementDTO]
    budget: BudgetDTO
    date_range: tuple[date, date]
    available_equipment: set[str] = Field(default_factory=set)
    catalog: list[CatalogProductDTO] = Field(default_factory=list)
    candidates: list[CandidateRecipeDTO] = Field(default_factory=list)
    pantry: list[PantryItemDTO] = Field(default_factory=list)
    favorites: set[str] = Field(default_factory=set)
    conversions: list[IngredientConversionDTO] = Field(default_factory=list)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    # Optional household-level per-day nutrition target. ``None`` (the default) means
    # no target: the optimizer's nutrition term is 0 and the plan is unchanged.
    nutrition_target: NutritionTargetDTO | None = None
    seed: int = 0
    as_of: date | None = None  # "now" for price-expiry checks; pure, never datetime.now()


# --------------------------------------------------------------------------- #
# Output DTOs
# --------------------------------------------------------------------------- #
class CostBreakdown(_Base):
    """Three views of a meal's cost (see OPTIMIZATION.md §3.4)."""

    total: DecimalStr
    imputable: DecimalStr
    marginal: DecimalStr


class PlannedMealDTO(_Base):
    """A recipe placed on a date/meal slot."""

    recipe_id: str
    title: str
    date: date
    meal_type: MealType
    servings: int
    participants: list[str]
    cost: CostBreakdown
    nutrition: NutritionDTO | None = None
    nutrition_complete: bool = True
    explanation: str = ""


class GroceryLineDTO(_Base):
    """One purchase line: what to buy, how much, what it costs, and its provenance."""

    canonical_name: str
    product_id: str | None
    display_name: str
    category: str = "uncategorized"
    needed_quantity: DecimalStr
    pantry_quantity: DecimalStr = Decimal("0")
    pending_quantity: DecimalStr = Decimal("0")
    packages_count: int = 0
    package_quantity: DecimalStr | None = None
    package_unit: str | None = None
    package_price: DecimalStr | None = None
    purchased_quantity: DecimalStr = Decimal("0")
    used_quantity: DecimalStr = Decimal("0")
    leftover: DecimalStr = Decimal("0")
    subtotal: DecimalStr = Decimal("0")
    subtotal_known: bool = False  # True = real price; False = estimated/absent
    availability: str = "unknown"
    source_type: str = "estimated"
    source_name: str = ""
    observed_at: date | None = None
    expired: bool = False


class CoverageCounts(_Base):
    """Line counts backing the coverage metrics."""

    with_price: int = 0
    without_price: int = 0
    estimated: int = 0
    expired: int = 0


class CoverageDTO(_Base):
    """Price-coverage metrics and status (OPTIMIZATION.md §6)."""

    price_coverage: DecimalStr
    weighted_price_coverage: DecimalStr
    status: CoverageStatus
    counts: CoverageCounts


class CostTotal(_Base):
    """Plan cost split into what is known vs estimated."""

    known: DecimalStr
    estimated: DecimalStr
    total: DecimalStr


class LeftoverDTO(_Base):
    """Surplus of a purchased product after the plan is cooked."""

    canonical_name: str
    product_id: str | None
    display_name: str
    quantity: DecimalStr
    unit: str


class PantryUsedDTO(_Base):
    """Quantity of a pantry item consumed by the plan."""

    canonical_name: str
    quantity: DecimalStr
    unit: str


MacroStatus = Literal["met", "under", "over", "unknown"]


class MacroSummaryDTO(_Base):
    """One macro's plan actual (per day) vs its target, with a coverage read."""

    actual_per_day: DecimalStr | None = None
    target_per_day: DecimalStr | None = None
    # actual_per_day - target_per_day (signed; positive = over target).
    deviation: DecimalStr | None = None
    # actual_per_day / target_per_day (1 = exactly on target).
    coverage_ratio: DecimalStr | None = None
    status: MacroStatus = "unknown"


class NutritionSummaryDTO(_Base):
    """Plan actual per-day macros vs the household target (present only with a target)."""

    days: int
    complete: bool  # True = every scheduled meal had usable nutrition data
    kcal: MacroSummaryDTO = Field(default_factory=MacroSummaryDTO)
    protein_g: MacroSummaryDTO = Field(default_factory=MacroSummaryDTO)
    carbs_g: MacroSummaryDTO = Field(default_factory=MacroSummaryDTO)
    fat_g: MacroSummaryDTO = Field(default_factory=MacroSummaryDTO)


class PlanResult(_Base):
    """A feasible, costed, auditable plan."""

    status: Literal["completed"] = "completed"
    planned_meals: list[PlannedMealDTO]
    grocery_lines: list[GroceryLineDTO]
    cost_per_day: dict[str, DecimalStr]
    cost_total: CostTotal
    budget_diff: DecimalStr
    leftovers: list[LeftoverDTO]
    pantry_used: list[PantryUsedDTO]
    coverage: CoverageDTO
    # Per-day macros vs the household nutrition target. ``None`` when no member set a
    # goal (behavior unchanged from before this feature).
    nutrition_summary: NutritionSummaryDTO | None = None
    warnings: list[str] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)
    seed: int = 0


class OffendingProduct(_Base):
    """A product that pushes the plan over budget."""

    canonical_name: str
    product_id: str | None
    display_name: str
    subtotal: DecimalStr


class InfeasibleResult(_Base):
    """No feasible plan exists — a diagnosis, never a fake plan (OPTIMIZATION.md §7)."""

    status: Literal["infeasible"] = "infeasible"
    minimal_conflict: list[str]
    min_budget_found: DecimalStr | None = None
    offending_products: list[OffendingProduct] = Field(default_factory=list)
    relaxable_soft_constraints: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    seed: int = 0
