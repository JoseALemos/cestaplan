"""Plan lifecycle service: create/enqueue, resolve, persist results, serialize.

Shared by the HTTP router (which enqueues work and reads results) and the worker
(which runs the engine and persists results). Keeps all money as ``Decimal`` in
the database and emits it as strings in API responses.
"""

from __future__ import annotations

import secrets
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.deps import HouseholdContext
from cestaplan_api.models import (
    GenerationJob,
    GroceryList,
    GroceryListItem,
    Household,
    HouseholdMember,
    Ingredient,
    IngredientProductMapping,
    MealPlan,
    MealRequirement,
    OptimizationRun,
    PlannedMeal,
    Product,
    ProductPrice,
    Recipe,
)
from cestaplan_engine import InfeasibleResult, PlanResult


def _now() -> datetime:
    return datetime.now(UTC)


def _new_seed() -> int:
    # Positive 31-bit seed, stored on the run so regeneration is reproducible.
    return secrets.randbelow(2_147_483_646) + 1


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Authorization / resolution (no IDOR: membership verified server-side)
# --------------------------------------------------------------------------- #
def resolve_plan(
    db: Session, user_id: int, meal_plan_id: uuid.UUID, *, require_edit: bool = False
) -> MealPlan:
    """Load a meal plan by public id and verify the caller belongs to its household."""
    meal_plan = db.execute(
        select(MealPlan).where(MealPlan.public_id == meal_plan_id)
    ).scalar_one_or_none()
    if meal_plan is None or meal_plan.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan no encontrado")
    _require_member(db, meal_plan.household_id, user_id, require_edit=require_edit)
    return meal_plan


def resolve_run(
    db: Session, user_id: int, run_id: uuid.UUID
) -> OptimizationRun:
    """Load an optimization run by public id and verify household membership."""
    run = db.execute(
        select(OptimizationRun).where(OptimizationRun.public_id == run_id)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Ejecución no encontrada")
    meal_plan = db.get(MealPlan, run.meal_plan_id)
    if meal_plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Ejecución no encontrada")
    _require_member(db, meal_plan.household_id, user_id)
    return run


def _require_member(
    db: Session, household_id: int, user_id: int, *, require_edit: bool = False
) -> HouseholdMember:
    member = db.execute(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    if member is None:
        # 404, not 403: never disclose the plan/household exists to outsiders.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Plan no encontrado")
    if require_edit and member.role not in ("owner", "editor"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Permisos insuficientes para esta acción"
        )
    return member


# --------------------------------------------------------------------------- #
# Create + enqueue
# --------------------------------------------------------------------------- #
def create_generation(
    db: Session,
    ctx: HouseholdContext,
    *,
    start_date,
    end_date,
    budget_amount: Decimal,
    currency: str,
    requirements: list[dict[str, Any]],
) -> tuple[MealPlan, OptimizationRun, GenerationJob]:
    """Create the plan, its requirements, a queued run and a queued job (async)."""
    meal_plan = MealPlan(
        household_id=ctx.household.id,
        retailer_id=ctx.household.default_retailer_id,
        store_id=ctx.household.default_store_id,
        start_date=start_date,
        end_date=end_date,
        budget_amount=budget_amount,
        currency=currency,
        status="generating",
    )
    db.add(meal_plan)
    db.flush()

    for req in requirements:
        db.add(MealRequirement(meal_plan_id=meal_plan.id, **req))
    db.flush()

    run, job = _enqueue_run(db, meal_plan, job_type="generate", seed=_new_seed())
    return meal_plan, run, job


def enqueue_regeneration(
    db: Session,
    meal_plan: MealPlan,
    *,
    job_type: str = "regenerate_plan",
    payload: dict[str, Any] | None = None,
) -> tuple[OptimizationRun, GenerationJob]:
    """Queue a fresh run (new seed) that re-runs generation for an existing plan."""
    meal_plan.status = "generating"
    run, job = _enqueue_run(
        db, meal_plan, job_type=job_type, seed=_new_seed(), payload=payload
    )
    return run, job


def _enqueue_run(
    db: Session,
    meal_plan: MealPlan,
    *,
    job_type: str,
    seed: int,
    payload: dict[str, Any] | None = None,
) -> tuple[OptimizationRun, GenerationJob]:
    settings = get_settings()
    run = OptimizationRun(
        meal_plan_id=meal_plan.id,
        status="queued",
        seed=seed,
        budget_amount=meal_plan.budget_amount,
    )
    db.add(run)
    db.flush()

    job = GenerationJob(
        optimization_run_id=run.id,
        meal_plan_id=meal_plan.id,
        job_type=job_type,
        status="queued",
        payload=payload,
        max_attempts=settings.worker_job_max_attempts,
        run_after=_now(),
    )
    db.add(job)
    db.flush()
    return run, job


def build_regenerate_meal_payload(
    db: Session, meal_plan: MealPlan, planned_meal: PlannedMeal
) -> dict[str, Any]:
    """Bias the next run to keep every other meal and replace only this slot.

    Uses only the public engine API: other meals' recipes become favorites (so the
    optimizer keeps them) and the target slot's current recipe is rejected (so the
    optimizer must choose an alternative for it).
    """
    others = db.execute(
        select(PlannedMeal.recipe_id).where(
            PlannedMeal.meal_plan_id == meal_plan.id,
            PlannedMeal.id != planned_meal.id,
        )
    ).scalars().all()
    return {
        "favorite_recipe_ids": [str(r) for r in others],
        "rejected_recipe_ids": [str(planned_meal.recipe_id)],
    }


# --------------------------------------------------------------------------- #
# Persist engine results (called by the worker)
# --------------------------------------------------------------------------- #
def _clear_previous(db: Session, meal_plan: MealPlan) -> None:
    """Remove any prior planned meals + grocery list so re-runs are idempotent."""
    lists = db.execute(
        select(GroceryList.id).where(GroceryList.meal_plan_id == meal_plan.id)
    ).scalars().all()
    if lists:
        db.execute(delete(GroceryListItem).where(GroceryListItem.grocery_list_id.in_(lists)))
        db.execute(delete(GroceryList).where(GroceryList.id.in_(lists)))
    db.execute(delete(PlannedMeal).where(PlannedMeal.meal_plan_id == meal_plan.id))
    db.flush()


def persist_plan_result(
    db: Session, meal_plan: MealPlan, run: OptimizationRun, result: PlanResult
) -> None:
    """Materialize a feasible plan: planned meals, grocery list, run summary."""
    _clear_previous(db, meal_plan)

    requirement_by_type = _requirement_by_type(db, meal_plan)
    for meal in result.planned_meals:
        db.add(
            PlannedMeal(
                meal_plan_id=meal_plan.id,
                meal_requirement_id=requirement_by_type.get(meal.meal_type),
                recipe_id=int(meal.recipe_id),
                scheduled_date=meal.date,
                meal_type=meal.meal_type,
                servings=meal.servings,
                status="planned",
            )
        )
    db.flush()

    _persist_grocery_list(db, meal_plan, result)

    run.result_summary = result.model_dump(mode="json")
    run.infeasibility_report = None
    run.status = "completed"
    run.finished_at = _now()
    meal_plan.status = "ready"


def persist_infeasible(
    db: Session, meal_plan: MealPlan, run: OptimizationRun, result: InfeasibleResult
) -> str:
    """Persist a diagnosis (never a fake plan) and mark the run/plan failed."""
    _clear_previous(db, meal_plan)
    run.result_summary = None
    run.infeasibility_report = result.model_dump(mode="json")
    run.status = "failed"
    run.finished_at = _now()
    meal_plan.status = "failed"
    reason = "; ".join(result.minimal_conflict) or "infeasible"
    return f"infeasible: {reason}"


def _requirement_by_type(db: Session, meal_plan: MealPlan) -> dict[str, int]:
    rows = db.execute(
        select(MealRequirement.meal_type, MealRequirement.id).where(
            MealRequirement.meal_plan_id == meal_plan.id
        )
    ).all()
    mapping: dict[str, int] = {}
    for meal_type, req_id in rows:
        mapping.setdefault(meal_type, req_id)
    return mapping


def _persist_grocery_list(
    db: Session, meal_plan: MealPlan, result: PlanResult
) -> None:
    coverage = result.coverage
    grocery = GroceryList(
        meal_plan_id=meal_plan.id,
        store_id=meal_plan.store_id,
        currency=meal_plan.currency,
        known_cost_amount=result.cost_total.known,
        estimated_cost_amount=result.cost_total.estimated,
        price_coverage=coverage.price_coverage,
        weighted_price_coverage=coverage.weighted_price_coverage,
        coverage_status=coverage.status,
    )
    db.add(grocery)
    db.flush()

    resolver = _LineResolver(db)
    for line in result.grocery_lines:
        product_id, price_id, unit_price = resolver.resolve(line)
        db.add(
            GroceryListItem(
                grocery_list_id=grocery.id,
                product_id=product_id,
                ingredient_id=resolver.ingredient_id(line.canonical_name),
                needed_quantity=line.needed_quantity,
                pantry_quantity=line.pantry_quantity,
                pending_quantity=line.pending_quantity,
                package_quantity=line.package_quantity,
                package_unit=line.package_unit,
                packages_selected=line.packages_count,
                purchased_quantity=line.purchased_quantity,
                used_quantity=line.used_quantity,
                leftover_quantity=line.leftover,
                unit_price=unit_price,
                price_product_price_id=price_id,
                total_cost=line.subtotal,
                price_status=_price_status(line),
                is_checked=False,
            )
        )
    db.flush()


def _price_status(line) -> str:
    if line.expired:
        return "stale"
    if line.subtotal_known:
        return "known"
    if line.subtotal > 0:
        return "estimated"
    return "missing"


class _LineResolver:
    """Maps a grocery line back to a concrete DB product + price by package.

    A grocery line names a canonical ingredient and the exact package it chose
    (quantity + unit). We match that back to the specific product/price row so the
    stored item carries a concrete product and its price provenance.
    """

    def __init__(self, db: Session) -> None:
        self._ingredient_id: dict[str, int] = {}
        # (canonical, unit, normalized_qty) -> (product_id, price_id, unit_price)
        self._by_package: dict[tuple[str, str, str], tuple[int, int | None, Decimal | None]] = {}

        latest = _latest_price_by_product(db)
        rows = db.execute(
            select(Ingredient, Product)
            .join(IngredientProductMapping, IngredientProductMapping.ingredient_id == Ingredient.id)
            .join(Product, Product.id == IngredientProductMapping.product_id)
            .where(IngredientProductMapping.is_active.is_(True))
        ).all()
        for ingredient, product in rows:
            self._ingredient_id.setdefault(ingredient.canonical_name, ingredient.id)
            price = latest.get(product.id)
            if price is None:
                continue
            key = (
                ingredient.canonical_name,
                price.package_unit,
                _norm(price.package_quantity),
            )
            self._by_package.setdefault(key, (product.id, price.id, price.unit_price))

    def ingredient_id(self, canonical_name: str) -> int | None:
        return self._ingredient_id.get(canonical_name)

    def resolve(self, line) -> tuple[int | None, int | None, Decimal | None]:
        if line.package_quantity is not None and line.package_unit is not None:
            key = (line.canonical_name, line.package_unit, _norm(line.package_quantity))
            hit = self._by_package.get(key)
            if hit is not None:
                return hit
        # Fall back to the representative product id carried on the line.
        product_id = int(line.product_id) if line.product_id is not None else None
        return product_id, None, None


def _latest_price_by_product(db: Session) -> dict[int, ProductPrice]:
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


def _norm(value: Decimal) -> str:
    return str(Decimal(value).normalize())


# --------------------------------------------------------------------------- #
# Serialization (money as strings)
# --------------------------------------------------------------------------- #
def serialize_run(db: Session, run: OptimizationRun) -> dict[str, Any]:
    job = db.execute(
        select(GenerationJob)
        .where(GenerationJob.optimization_run_id == run.id)
        .order_by(GenerationJob.id.desc())
    ).scalars().first()
    data: dict[str, Any] = {
        "optimization_run_id": str(run.public_id),
        "meal_plan_id": _meal_plan_public_id(db, run.meal_plan_id),
        "status": run.status,
        "seed": run.seed,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "job": None,
    }
    if job is not None:
        data["job"] = {
            "status": job.status,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "run_after": job.run_after,
            "locked_by": job.locked_by,
            "heartbeat_at": job.heartbeat_at,
            "last_error": job.last_error,
        }
    if run.status == "failed" and run.infeasibility_report is not None:
        data["infeasibility"] = run.infeasibility_report
    return data


def _meal_plan_public_id(db: Session, meal_plan_id: int) -> str | None:
    mp = db.get(MealPlan, meal_plan_id)
    return str(mp.public_id) if mp is not None else None


def _latest_run(db: Session, meal_plan: MealPlan) -> OptimizationRun | None:
    return db.execute(
        select(OptimizationRun)
        .where(OptimizationRun.meal_plan_id == meal_plan.id)
        .order_by(OptimizationRun.id.desc())
    ).scalars().first()


def serialize_plan(db: Session, meal_plan: MealPlan) -> dict[str, Any]:
    """Full persisted plan: meals + per-recipe cost/nutrition + totals + coverage."""
    run = _latest_run(db, meal_plan)
    base: dict[str, Any] = {
        "id": str(meal_plan.public_id),
        "status": meal_plan.status,
        "start_date": meal_plan.start_date,
        "end_date": meal_plan.end_date,
        "budget": {
            "amount": _s(meal_plan.budget_amount),
            "currency": meal_plan.currency,
        },
        "run": None,
        "planned_meals": [],
        "totals": None,
        "budget_diff": None,
        "coverage": None,
        "warnings": [],
        "explanations": [],
        "grocery_summary": None,
    }
    if run is None:
        return base

    base["run"] = {"id": str(run.public_id), "status": run.status, "seed": run.seed}
    if run.status == "failed" and run.infeasibility_report is not None:
        base["infeasibility"] = run.infeasibility_report
        return base

    summary = run.result_summary
    if not summary:
        return base

    meals_db = db.execute(
        select(PlannedMeal)
        .where(PlannedMeal.meal_plan_id == meal_plan.id)
        .order_by(PlannedMeal.id)
    ).scalars().all()
    recipe_public = _recipe_public_ids(db, [m.recipe_id for m in meals_db])

    planned: list[dict[str, Any]] = []
    for db_meal, dto in zip(meals_db, summary.get("planned_meals", []), strict=False):
        planned.append(
            {
                "id": str(db_meal.public_id),
                "recipe_id": recipe_public.get(db_meal.recipe_id),
                "title": dto.get("title"),
                "date": dto.get("date"),
                "meal_type": dto.get("meal_type"),
                "servings": dto.get("servings"),
                "status": db_meal.status,
                "cost": dto.get("cost"),
                "nutrition": dto.get("nutrition"),
                "nutrition_complete": dto.get("nutrition_complete"),
                "explanation": dto.get("explanation"),
            }
        )

    base["planned_meals"] = planned
    base["totals"] = {
        "cost_total": summary.get("cost_total"),
        "cost_per_day": summary.get("cost_per_day"),
    }
    base["budget_diff"] = summary.get("budget_diff")
    base["coverage"] = summary.get("coverage")
    base["warnings"] = summary.get("warnings", [])
    base["explanations"] = summary.get("explanations", [])
    base["grocery_summary"] = _grocery_summary(db, meal_plan)
    return base


def _recipe_public_ids(db: Session, recipe_ids: list[int]) -> dict[int, str]:
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(Recipe.id, Recipe.public_id).where(Recipe.id.in_(set(recipe_ids)))
    ).all()
    return {rid: str(pid) for rid, pid in rows}


def _grocery_summary(db: Session, meal_plan: MealPlan) -> dict[str, Any] | None:
    grocery = db.execute(
        select(GroceryList).where(GroceryList.meal_plan_id == meal_plan.id)
    ).scalar_one_or_none()
    if grocery is None:
        return None
    count = db.execute(
        select(GroceryListItem.id).where(GroceryListItem.grocery_list_id == grocery.id)
    ).scalars().all()
    return {
        "items": len(count),
        "known_cost": _s(grocery.known_cost_amount),
        "estimated_cost": _s(grocery.estimated_cost_amount),
        "coverage_status": grocery.coverage_status,
    }


def serialize_grocery_list(db: Session, meal_plan: MealPlan) -> dict[str, Any]:
    """Consolidated grocery list grouped by category (money as strings)."""
    grocery = db.execute(
        select(GroceryList).where(GroceryList.meal_plan_id == meal_plan.id)
    ).scalar_one_or_none()
    if grocery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lista de compra no encontrada")

    items = db.execute(
        select(GroceryListItem)
        .where(GroceryListItem.grocery_list_id == grocery.id)
        .order_by(GroceryListItem.id)
    ).scalars().all()

    ingredient_ids = [i.ingredient_id for i in items if i.ingredient_id is not None]
    product_ids = [i.product_id for i in items if i.product_id is not None]
    ingredients = _ingredient_map(db, ingredient_ids)
    products = _product_map(db, product_ids)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        ing = ingredients.get(item.ingredient_id) if item.ingredient_id is not None else None
        prod = products.get(item.product_id) if item.product_id is not None else None
        category = (ing.category_code if ing else None) or "uncategorized"
        groups[category].append(
            {
                "id": str(item.public_id),
                "generic_name": ing.display_name if ing else None,
                "product_name": prod.name if prod else None,
                "needed_quantity": _s(item.needed_quantity),
                "pending_quantity": _s(item.pending_quantity),
                "pantry_available": (item.pantry_quantity or Decimal("0")) > 0,
                "packages_count": item.packages_selected,
                "package_quantity": _s(item.package_quantity),
                "package_unit": item.package_unit,
                "unit_price": _s(item.unit_price),
                "subtotal": _s(item.total_cost),
                "subtotal_known": item.price_status == "known",
                "price_status": item.price_status,
                "availability": _availability(db, item),
                "source": _source(db, item),
                "is_checked": item.is_checked,
            }
        )

    return {
        "meal_plan_id": str(meal_plan.public_id),
        "currency": grocery.currency,
        "coverage_status": grocery.coverage_status,
        "known_cost": _s(grocery.known_cost_amount),
        "estimated_cost": _s(grocery.estimated_cost_amount),
        "categories": [
            {"category": cat, "items": groups[cat]} for cat in sorted(groups)
        ],
    }


def _item_price(db: Session, item: GroceryListItem) -> ProductPrice | None:
    if item.price_product_price_id is None:
        return None
    return db.get(ProductPrice, item.price_product_price_id)


def _availability(db: Session, item: GroceryListItem) -> str | None:
    price = _item_price(db, item)
    return price.availability if price else None


def _source(db: Session, item: GroceryListItem) -> dict[str, Any] | None:
    price = _item_price(db, item)
    if price is None:
        return None
    return {
        "source_type": price.source_type,
        "source_name": price.source_name,
        "observed_at": price.observed_at,
    }


def _ingredient_map(db: Session, ids: list[int]) -> dict[int, Ingredient]:
    if not ids:
        return {}
    rows = db.execute(select(Ingredient).where(Ingredient.id.in_(set(ids)))).scalars().all()
    return {i.id: i for i in rows}


def _product_map(db: Session, ids: list[int]) -> dict[int, Product]:
    if not ids:
        return {}
    rows = db.execute(select(Product).where(Product.id.in_(set(ids)))).scalars().all()
    return {p.id: p for p in rows}


def household_for_recipe_scope(
    db: Session, household_public_id: uuid.UUID, user_id: int
) -> Household:
    """Resolve a household by public id and verify membership (favorites/feedback)."""
    household = db.execute(
        select(Household).where(Household.public_id == household_public_id)
    ).scalar_one_or_none()
    if household is None or household.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Hogar no encontrado")
    _require_member(db, household.id, user_id)
    return household
