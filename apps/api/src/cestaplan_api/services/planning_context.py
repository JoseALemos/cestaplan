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

from cestaplan_api.config import Settings, get_settings
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
    RecipeFeedback,
)
from cestaplan_api.services.candidate_providers import (
    CandidateRequest,
    get_candidate_provider,
)
from cestaplan_engine import (
    BudgetDTO,
    CatalogProductDTO,
    IngredientConversionDTO,
    MealRequirementDTO,
    MemberDTO,
    NutritionDTO,
    NutritionTargetDTO,
    PackageOptionDTO,
    PantryItemDTO,
    PlanInput,
)


def build_plan_input(
    db: Session,
    meal_plan: MealPlan,
    *,
    seed: int,
    as_of: date | None = None,
    settings: Settings | None = None,
    warnings: list[str] | None = None,
    operation: str = "plan_generation",
    optimization_run_id: int | None = None,
    user_id: int | None = None,
) -> PlanInput:
    """Assemble the engine's :class:`PlanInput` for a persisted meal plan.

    Candidate recipes come from the configured provider (OpenAI when AI is enabled,
    the seed library otherwise). Any AI degradation is appended to ``warnings`` when a
    sink list is supplied, so the worker can surface it on the plan result.
    """
    settings = settings or get_settings()
    household_id = meal_plan.household_id
    effective_as_of = as_of or date.today()

    rejected = _rejected_recipe_ids(db, household_id)
    members = _build_members(db, household_id, rejected)
    requirements = _build_requirements(meal_plan)
    requested_types = {r.meal_type for r in requirements if r.requested_count > 0}

    budget = BudgetDTO(
        amount=meal_plan.budget_amount or Decimal("0"),
        currency=meal_plan.currency,
        priority=meal_plan.budget_priority or "waste",  # type: ignore[arg-type]
    )

    equipment = _build_equipment(db, household_id)
    catalog = _build_catalog(db, meal_plan.retailer_id)

    provider = get_candidate_provider(settings)
    bundle = provider.get_candidates(
        db,
        CandidateRequest(
            household_id=household_id,
            requested_types=requested_types,
            allow_list=sorted({c.canonical_name for c in catalog}),
            allergens={a for m in members for a in m.allergens},
            hard_restrictions={h for m in members for h in m.hard_restrictions},
            soft_preferences=[p for m in members for p in m.soft_preferences],
            equipment=equipment,
            budget_amount=budget.amount,
            currency=budget.currency,
            requirement_counts={
                r.meal_type: r.requested_count
                for r in requirements
                if r.requested_count > 0
            },
            user_id=user_id,
            operation=operation,
            optimization_run_id=optimization_run_id,
        ),
    )
    if warnings is not None:
        warnings.extend(bundle.warnings)

    return PlanInput(
        members=members,
        meal_requirements=requirements,
        budget=budget,
        date_range=(meal_plan.start_date, meal_plan.end_date),
        available_equipment=equipment,
        catalog=catalog,
        candidates=bundle.candidates,
        pantry=_build_pantry(db, household_id),
        favorites=_build_favorites(db, household_id),
        conversions=_build_conversions(db),
        nutrition_target=_build_nutrition_target(db, household_id),
        seed=seed,
        as_of=effective_as_of,
    )


# --------------------------------------------------------------------------- #
# Nutrition target
# --------------------------------------------------------------------------- #
# Engine macro -> DietaryProfile column. Targets are per-day, per-person goals.
_TARGET_COLUMNS = (
    ("kcal", "energy_target_kcal"),
    ("protein_g", "protein_target_g"),
    ("carbs_g", "carb_target_g"),
    ("fat_g", "fat_target_g"),
)


def _build_nutrition_target(db: Session, household_id: int) -> NutritionTargetDTO | None:
    """Aggregate eater members' per-day nutrition goals into a household target.

    Each member's per-person daily goal is scaled by ``relative_serving`` (their
    portion size) and summed, giving the household's total per-day target that the
    engine compares against the plan's actual per-day nutrition. A macro no member
    set stays ``None``; if no member set any goal the target is ``None`` (the engine
    then scores no nutrition term and the plan is unchanged).
    """
    members = db.execute(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.is_eater.is_(True),
        )
    ).scalars().all()

    totals: dict[str, Decimal] = {macro: Decimal("0") for macro, _ in _TARGET_COLUMNS}
    seen: dict[str, bool] = {macro: False for macro, _ in _TARGET_COLUMNS}
    for member in members:
        profile = member.dietary_profiles[0] if member.dietary_profiles else None
        if profile is None:
            continue
        serving = member.relative_serving or Decimal("1")
        for macro, column in _TARGET_COLUMNS:
            value = getattr(profile, column)
            if value is not None:
                totals[macro] += value * serving
                seen[macro] = True

    if not any(seen.values()):
        return None
    return NutritionTargetDTO(
        kcal=totals["kcal"] if seen["kcal"] else None,
        protein_g=totals["protein_g"] if seen["protein_g"] else None,
        carbs_g=totals["carbs_g"] if seen["carbs_g"] else None,
        fat_g=totals["fat_g"] if seen["fat_g"] else None,
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
def _latest_prices(db: Session, retailer_id: int | None) -> dict[int, ProductPrice]:
    """Most recent :class:`ProductPrice` per product WITHIN a single retailer (chain).

    History is append-only, so we take the newest observation per product aggregated over
    ALL of the chain's stores (the specific store is irrelevant: the plan is priced against
    the selected chain). Prices are scoped to ``retailer_id`` and never mixed across chains:
    a product with no price anywhere in the chain is simply absent (the catalog/coverage then
    reflect it as without_price).

    A plan is always calculated against a single chain; ``retailer_id=None`` never authorizes
    mixing prices. With no chain resolved this returns an EMPTY catalogue (the planner preflight
    stops on ``no_retailer_selected`` before reaching here — see ``planner_preflight``). Any
    cross-chain global analysis must live in a separate, explicitly-named admin function.
    """
    if retailer_id is None:
        return {}
    stmt = select(ProductPrice).where(ProductPrice.retailer_id == retailer_id)
    rows = db.execute(
        stmt.order_by(
            ProductPrice.product_id,
            ProductPrice.observed_at.desc(),
            ProductPrice.id.desc(),
        )
    ).scalars().all()
    latest: dict[int, ProductPrice] = {}
    for price in rows:
        latest.setdefault(price.product_id, price)
    return latest


def _build_conversions(db: Session) -> list[IngredientConversionDTO]:
    """Density-based ml<->g conversions for ingredients that declare a density.

    Lets the engine price a recipe measured in one dimension against a product
    packaged in the other (e.g. a recipe's ``ml`` of milk against a product sold
    by ``g``) instead of leaving the line unpriced. It never guesses: only
    ingredients with an explicit ``density_g_per_ml`` contribute, and the
    :class:`UnitConverter` derives the reverse (g->ml) from each factor.
    """
    rows = db.execute(
        select(Ingredient.canonical_name, Ingredient.density_g_per_ml).where(
            Ingredient.density_g_per_ml.is_not(None)
        )
    ).all()
    return [
        IngredientConversionDTO(
            canonical_name=name,
            from_unit="ml",
            to_unit="g",
            factor=density,
        )
        for name, density in rows
        if density is not None and density > 0
    ]


def _build_catalog(db: Session, retailer_id: int | None) -> list[CatalogProductDTO]:
    latest = _latest_prices(db, retailer_id)
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
