"""Concise DTO builders for engine tests. Decimals only — never floats."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_engine.contracts import (
    BudgetDTO,
    CandidateRecipeDTO,
    CatalogProductDTO,
    MealRequirementDTO,
    MemberDTO,
    NutritionDTO,
    PackageOptionDTO,
    PantryItemDTO,
    PlanInput,
    RecipeIngredientDTO,
)

AS_OF = date(2026, 7, 21)
RANGE = (date(2026, 7, 20), date(2026, 7, 26))


def D(value: str | int) -> Decimal:
    return Decimal(str(value))


def package(
    product_id: str,
    qty: str,
    unit: str,
    price: str,
    *,
    has_price: bool = True,
    observed_at: date | None = date(2026, 7, 1),
    expires_at: date | None = date(2026, 8, 1),
    source_type: str = "demo",
    source_name: str = "DemoStore",
) -> PackageOptionDTO:
    return PackageOptionDTO(
        product_id=product_id,
        package_quantity=D(qty),
        package_unit=unit,
        amount=D(price),
        unit_price=D("0"),
        availability="in_stock",
        source_type=source_type,
        source_name=source_name,
        observed_at=observed_at,
        expires_at=expires_at,
        confidence_score=D("0.9"),
        has_price=has_price,
    )


def product(
    product_id: str,
    canonical: str,
    packages: list[PackageOptionDTO],
    *,
    category: str = "misc",
    nutrition: NutritionDTO | None = None,
    allergens: set[str] | None = None,
) -> CatalogProductDTO:
    return CatalogProductDTO(
        product_id=product_id,
        canonical_name=canonical,
        display_name=product_id,
        category=category,
        packages=packages,
        nutrition=nutrition,
        allergens=allergens or set(),
    )


def ingredient(
    canonical: str, qty: str, unit: str, *, optional: bool = False, group: str | None = None
) -> RecipeIngredientDTO:
    return RecipeIngredientDTO(
        canonical_name=canonical,
        display_name=canonical,
        quantity=D(qty),
        unit=unit,
        optional=optional,
        substitution_group=group,
    )


def recipe(
    recipe_id: str,
    meal_types: set[str],
    ingredients: list[RecipeIngredientDTO],
    *,
    servings: int = 2,
    title: str | None = None,
    prep: int = 10,
    cook: int = 15,
    allergens: set[str] | None = None,
    equipment: set[str] | None = None,
    tags: list[str] | None = None,
    leftover_reuse: bool = False,
) -> CandidateRecipeDTO:
    return CandidateRecipeDTO(
        recipe_id=recipe_id,
        title=title or recipe_id,
        servings=servings,
        meal_types=meal_types,  # type: ignore[arg-type]
        preference_tags=tags or [],
        ingredients=ingredients,
        preparation_minutes=prep,
        cooking_minutes=cook,
        required_equipment=equipment or set(),
        allergens_declared=allergens or set(),
        leftover_reuse=leftover_reuse,
    )


def member(
    alias: str,
    *,
    allergens: set[str] | None = None,
    hard: set[str] | None = None,
    soft: list[str] | None = None,
    rejected: set[str] | None = None,
) -> MemberDTO:
    return MemberDTO(
        alias=alias,
        relative_serving=D("1"),
        allergens=allergens or set(),
        hard_restrictions=hard or set(),
        soft_preferences=soft or [],
        rejected_recipe_ids=rejected or set(),
    )


def requirement(
    meal_type: str, count: int, servings: int = 2, **kwargs
) -> MealRequirementDTO:
    return MealRequirementDTO(
        meal_type=meal_type,  # type: ignore[arg-type]
        requested_count=count,
        default_servings=servings,
        **kwargs,
    )


def budget(amount: str, *, strict: bool = True, margin: str = "0") -> BudgetDTO:
    return BudgetDTO(amount=D(amount), strict=strict, max_margin_ratio=D(margin))


def plan_input(
    *,
    members: list[MemberDTO],
    requirements: list[MealRequirementDTO],
    catalog: list[CatalogProductDTO],
    candidates: list[CandidateRecipeDTO],
    budget_amount: str = "100",
    strict: bool = True,
    pantry: list[PantryItemDTO] | None = None,
    favorites: set[str] | None = None,
    conversions=None,
    seed: int = 42,
    equipment: set[str] | None = None,
    weights=None,
    as_of: date = AS_OF,
    date_range: tuple[date, date] = RANGE,
) -> PlanInput:
    kwargs = {}
    if weights is not None:
        kwargs["weights"] = weights
    return PlanInput(
        members=members,
        meal_requirements=requirements,
        budget=budget(budget_amount, strict=strict),
        date_range=date_range,
        available_equipment=equipment or set(),
        catalog=catalog,
        candidates=candidates,
        pantry=pantry or [],
        favorites=favorites or set(),
        conversions=conversions or [],
        seed=seed,
        as_of=as_of,
        **kwargs,
    )
