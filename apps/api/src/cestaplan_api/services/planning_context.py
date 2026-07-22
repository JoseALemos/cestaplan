"""DB -> engine adapter: build a :class:`PlanInput` from persisted rows.

The deterministic engine speaks only in plain-data DTOs (never SQLAlchemy). This
module reads a household + a persisted :class:`MealPlan` and assembles the
:class:`PlanInput` the engine consumes. Money and quantities stay ``Decimal``.

Mapping notes (see the vertical-slice spec):
- Member allergens come from :class:`Allergy` rows; hard restrictions from the diet
  type plus ``avoid`` food preferences; soft preferences from ``like``/``dislike``
  preferences; rejected recipes from :class:`RecipeFeedback` (``reject``/``no_show``).
- One :class:`CatalogProductDTO` per canonical ingredient carries one
  :class:`PackageOptionDTO` per mapped product (its most recent price).
- Candidates ARE the seeded recipes filtered to the requested meal types (AI is
  disabled in the slice); recipe allergens are derived from their ingredients.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Equipment,
    FavoriteRecipe,
    HouseholdMember,
    Ingredient,
    IngredientProductMapping,
    MealPlan,
    PantryItem,
    Product,
    ProductPrice,
    Recipe,
    RecipeFeedback,
)
from cestaplan_engine import (
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


def build_plan_input(
    db: Session,
    meal_plan: MealPlan,
    *,
    seed: int,
    as_of: date | None = None,
) -> PlanInput:
    """Assemble the engine's :class:`PlanInput` for a persisted meal plan."""
    household_id = meal_plan.household_id
    effective_as_of = as_of or date.today()

    rejected = _rejected_recipe_ids(db, household_id)
    members = _build_members(db, household_id, rejected)
    requirements = _build_requirements(meal_plan)
    requested_types = {r.meal_type for r in requirements if r.requested_count > 0}

    budget = BudgetDTO(
        amount=meal_plan.budget_amount or Decimal("0"),
        currency=meal_plan.currency,
    )

    return PlanInput(
        members=members,
        meal_requirements=requirements,
        budget=budget,
        date_range=(meal_plan.start_date, meal_plan.end_date),
        available_equipment=_build_equipment(db, household_id),
        catalog=_build_catalog(db),
        candidates=_build_candidates(db, requested_types),
        pantry=_build_pantry(db, household_id),
        favorites=_build_favorites(db, household_id),
        conversions=[],
        seed=seed,
        as_of=effective_as_of,
    )


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #
def _rejected_recipe_ids(db: Session, household_id: int) -> set[str]:
    rows = db.execute(
        select(RecipeFeedback.recipe_id).where(
            RecipeFeedback.household_id == household_id,
            RecipeFeedback.sentiment.in_(("reject", "no_show")),
        )
    ).scalars().all()
    return {str(rid) for rid in rows}


def _build_members(
    db: Session, household_id: int, rejected: set[str]
) -> list[MemberDTO]:
    members = db.execute(
        select(HouseholdMember)
        .where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.is_eater.is_(True),
        )
        .order_by(HouseholdMember.joined_at, HouseholdMember.id)
    ).scalars().all()

    dtos: list[MemberDTO] = []
    seen_aliases: set[str] = set()
    for member in members:
        profile = member.dietary_profiles[0] if member.dietary_profiles else None
        allergens: set[str] = set()
        hard: set[str] = set()
        soft: list[str] = []
        if profile is not None:
            for allergy in profile.allergies:
                allergens.add(allergy.allergen_code)
            if profile.diet_type:
                hard.add(profile.diet_type)
            for pref in profile.food_preferences:
                if pref.sentiment == "avoid":
                    hard.add(pref.subject_ref)
                elif pref.sentiment == "dislike":
                    soft.append(f"avoid:{pref.subject_ref}")
                else:  # like
                    soft.append(pref.subject_ref)

        dtos.append(
            MemberDTO(
                alias=_unique_alias(member, seen_aliases),
                relative_serving=member.relative_serving,
                allergens=allergens,
                hard_restrictions=hard,
                soft_preferences=soft,
                rejected_recipe_ids=set(rejected),
            )
        )
    return dtos


def _unique_alias(member: HouseholdMember, seen: set[str]) -> str:
    base = member.display_name or f"comensal-{member.public_id.hex[:8]}"
    alias = base
    suffix = 2
    while alias in seen:
        alias = f"{base}-{suffix}"
        suffix += 1
    seen.add(alias)
    return alias


# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #
def _build_requirements(meal_plan: MealPlan) -> list[MealRequirementDTO]:
    dtos: list[MealRequirementDTO] = []
    for req in meal_plan.requirements:
        dtos.append(
            MealRequirementDTO(
                meal_type=req.meal_type,  # type: ignore[arg-type]
                requested_count=req.requested_count,
                default_servings=req.default_servings,
                selected_dates=req.selected_dates,
                auto_distribute=req.auto_distribute,
                preferred_days=req.preferred_days,  # type: ignore[arg-type]
                maximum_preparation_minutes=req.maximum_preparation_minutes,
                requires_tupper=req.requires_tupper,
                reheating_available=req.reheating_available,
            )
        )
    return dtos


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def _latest_prices(db: Session) -> dict[int, ProductPrice]:
    """Most recent :class:`ProductPrice` per product (append-only history)."""
    rows = db.execute(
        select(ProductPrice).order_by(
            ProductPrice.product_id,
            ProductPrice.observed_at.desc(),
            ProductPrice.id.desc(),
        )
    ).scalars().all()
    latest: dict[int, ProductPrice] = {}
    for price in rows:
        latest.setdefault(price.product_id, price)
    return latest


def _build_catalog(db: Session) -> list[CatalogProductDTO]:
    latest = _latest_prices(db)
    rows = db.execute(
        select(IngredientProductMapping, Product)
        .join(Product, Product.id == IngredientProductMapping.product_id)
        .where(
            IngredientProductMapping.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
        .order_by(
            IngredientProductMapping.ingredient_id,
            IngredientProductMapping.preference_rank,
        )
    ).all()

    products_by_ingredient: dict[int, list[Product]] = defaultdict(list)
    for mapping, product in rows:
        products_by_ingredient[mapping.ingredient_id].append(product)

    catalog: list[CatalogProductDTO] = []
    for ingredient_id, products in products_by_ingredient.items():
        ingredient = db.get(Ingredient, ingredient_id)
        if ingredient is None:
            continue
        allergens: set[str] = set(ingredient.allergen_codes or [])
        nutrition: NutritionDTO | None = None
        packages: list[PackageOptionDTO] = []
        for product in products:
            price = latest.get(product.id)
            if price is None:
                continue
            packages.append(_package_option(product, price))
            nutr = product.nutrition
            if nutr is not None:
                allergens |= set(nutr.allergens or [])
                if nutrition is None:
                    nutrition = NutritionDTO(
                        kcal=nutr.energy_kcal,
                        protein_g=nutr.protein_g,
                        carbs_g=nutr.carbohydrate_g,
                        fat_g=nutr.fat_g,
                    )
        if not packages:
            continue
        catalog.append(
            CatalogProductDTO(
                product_id=str(products[0].id),
                canonical_name=ingredient.canonical_name,
                display_name=ingredient.display_name,
                category=ingredient.category_code or "uncategorized",
                packages=packages,
                nutrition=nutrition,
                allergens=allergens,
            )
        )
    return catalog


def _package_option(product: Product, price: ProductPrice) -> PackageOptionDTO:
    return PackageOptionDTO(
        product_id=str(product.id),
        package_quantity=price.package_quantity,
        package_unit=price.package_unit,
        amount=price.amount,
        unit_price=price.unit_price or Decimal("0"),
        availability=price.availability or "unknown",  # type: ignore[arg-type]
        source_type=price.source_type,
        source_name=price.source_name,
        observed_at=price.observed_at.date() if price.observed_at else None,
        expires_at=price.expires_at.date() if price.expires_at else None,
        confidence_score=price.confidence_score,
        has_price=True,
    )


# --------------------------------------------------------------------------- #
# Candidates (seeded recipes)
# --------------------------------------------------------------------------- #
def _build_candidates(
    db: Session, requested_types: set[str]
) -> list[CandidateRecipeDTO]:
    ingredient_allergens = {
        ing_id: set(codes or [])
        for ing_id, codes in db.execute(
            select(Ingredient.id, Ingredient.allergen_codes)
        ).all()
    }

    recipes = db.execute(
        select(Recipe)
        .where(Recipe.deleted_at.is_(None), Recipe.is_public.is_(True))
        .order_by(Recipe.id)
    ).scalars().all()

    candidates: list[CandidateRecipeDTO] = []
    for recipe in recipes:
        meal_types = set(recipe.meal_types or [])
        if requested_types and not (meal_types & requested_types):
            continue

        ingredients: list[RecipeIngredientDTO] = []
        declared: set[str] = set()
        for ri in recipe.ingredients:
            declared |= ingredient_allergens.get(ri.ingredient_id, set())
            ingredients.append(
                RecipeIngredientDTO(
                    canonical_name=ri.canonical_name,
                    display_name=ri.display_name or ri.canonical_name,
                    quantity=ri.quantity,
                    unit=ri.unit,
                    optional=ri.optional,
                    substitution_group=ri.substitution_group,
                )
            )

        candidates.append(
            CandidateRecipeDTO(
                recipe_id=str(recipe.id),
                title=recipe.title,
                description=recipe.description or "",
                servings=recipe.servings,
                meal_types=meal_types,  # type: ignore[arg-type]
                cuisine=recipe.cuisine or "",
                preference_tags=list(recipe.preference_tags or []),
                ingredients=ingredients,
                steps=[s.instruction for s in sorted(recipe.steps, key=lambda s: s.step_number)],
                preparation_minutes=recipe.preparation_minutes or 0,
                cooking_minutes=recipe.cooking_minutes or 0,
                required_equipment=set(recipe.required_equipment or []),
                allergens_declared=declared,
            )
        )
    return candidates


# --------------------------------------------------------------------------- #
# Pantry / favorites / equipment
# --------------------------------------------------------------------------- #
def _build_pantry(db: Session, household_id: int) -> list[PantryItemDTO]:
    items = db.execute(
        select(PantryItem).where(
            PantryItem.household_id == household_id,
            PantryItem.deleted_at.is_(None),
        )
    ).scalars().all()

    dtos: list[PantryItemDTO] = []
    for item in items:
        if item.ingredient_id is None:
            continue
        ingredient = db.get(Ingredient, item.ingredient_id)
        if ingredient is None:
            continue
        dtos.append(
            PantryItemDTO(
                canonical_name=ingredient.canonical_name,
                quantity=item.quantity,
                unit=item.unit,
                expires_at=item.expires_at.date() if item.expires_at else None,
            )
        )
    return dtos


def _build_favorites(db: Session, household_id: int) -> set[str]:
    rows = db.execute(
        select(FavoriteRecipe.recipe_id).where(
            FavoriteRecipe.household_id == household_id
        )
    ).scalars().all()
    return {str(rid) for rid in rows}


def _build_equipment(db: Session, household_id: int) -> set[str]:
    rows = db.execute(
        select(Equipment.equipment_code).where(
            Equipment.household_id == household_id,
            Equipment.available.is_(True),
        )
    ).scalars().all()
    return set(rows)
