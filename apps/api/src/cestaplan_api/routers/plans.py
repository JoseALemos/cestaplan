"""Plans router (prefix ``/api/v1/plans``): async generation, results, feedback.

Generation is asynchronous: ``POST /generate`` persists the plan + a queued job and
returns 202 with a status URL; the worker runs the deterministic engine. Every route
verifies household membership server-side (no IDOR); money is returned as strings.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from cestaplan_api.config import get_settings
from cestaplan_api.deps import (
    CurrentUser,
    DbSession,
    HouseholdCtx,
    get_household_context,
    rate_limit,
    verify_csrf,
)
from cestaplan_api.models import (
    FavoriteRecipe,
    GroceryList,
    Household,
    MealPlan,
    PlannedMeal,
    Recipe,
    RecipeFeedback,
    Retailer,
)
from cestaplan_api.schemas.plan import FeedbackRequest, FeedbackSentiment, GenerateRequest
from cestaplan_api.security import plan_generation_rate_limiter
from cestaplan_api.services.audit import record_audit
from cestaplan_api.services.plan_comparison import compare_plan_with_travel
from cestaplan_api.services.plan_service import (
    build_regenerate_meal_payload,
    create_generation,
    duplicate_generation,
    enqueue_regeneration,
    resolve_plan,
    resolve_plan_retailer,
    resolve_run,
    serialize_plan,
    serialize_run,
)
from cestaplan_api.services.quota import check_generation_quota

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


def _status_url(run_public_id: uuid.UUID) -> str:
    return f"/api/v1/plans/runs/{run_public_id}"


def _resolve_recipe(db: DbSession, recipe_id: uuid.UUID) -> Recipe:
    recipe = db.execute(
        select(Recipe).where(Recipe.public_id == recipe_id)
    ).scalar_one_or_none()
    if recipe is None or recipe.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Receta no encontrada")
    return recipe


def _serialize_recipe_brief(recipe: Recipe) -> dict:
    """Minimal recipe info for favorites/feedback list rows (not the full detail)."""
    return {
        "recipe_id": str(recipe.public_id),
        "title": recipe.title,
        "meal_types": list(recipe.meal_types or []),
        "cuisine": recipe.cuisine,
        "preparation_minutes": recipe.preparation_minutes,
        "cooking_minutes": recipe.cooking_minutes,
        "tags": list(recipe.preference_tags or []),
    }


# --------------------------------------------------------------------------- #
# History (list)
# --------------------------------------------------------------------------- #
@router.get("")
def list_plans(ctx: HouseholdCtx, user: CurrentUser, db: DbSession) -> list[dict]:
    """List the household's meal plans, newest first. Needs ``?household_id=``.

    Excludes soft-deleted plans. Light rows only (no full costing/meals) — use
    ``GET /{meal_plan_id}`` for the full persisted plan.
    """
    meal_count = (
        select(func.count(PlannedMeal.id))
        .where(PlannedMeal.meal_plan_id == MealPlan.id)
        .correlate(MealPlan)
        .scalar_subquery()
    )

    def _latest_grocery(column):
        # Cost of the most recent grocery list for the plan (regeneration writes a new one).
        return (
            select(column)
            .where(GroceryList.meal_plan_id == MealPlan.id)
            .correlate(MealPlan)
            .order_by(GroceryList.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )

    cost_known = _latest_grocery(GroceryList.known_cost_amount)
    cost_estimated = _latest_grocery(GroceryList.estimated_cost_amount)
    rows = db.execute(
        select(
            MealPlan,
            Retailer,
            meal_count.label("meal_count"),
            cost_known.label("cost_known"),
            cost_estimated.label("cost_estimated"),
        )
        .outerjoin(Retailer, Retailer.id == MealPlan.retailer_id)
        .where(
            MealPlan.household_id == ctx.household.id,
            MealPlan.deleted_at.is_(None),
        )
        .order_by(MealPlan.created_at.desc(), MealPlan.id.desc())
    ).all()
    return [
        {
            "id": str(plan.public_id),
            "status": plan.status,
            "start_date": plan.start_date,
            "end_date": plan.end_date,
            "created_at": plan.created_at,
            "budget_amount": str(plan.budget_amount) if plan.budget_amount is not None else None,
            "currency": plan.currency,
            "meal_count": meal_count_value,
            "retailer_name": retailer.name if retailer is not None else None,
            # Persisted cost of the plan's grocery list (None until a plan is generated).
            "cost_known": str(known) if known is not None else None,
            "cost_estimated": str(estimated) if estimated is not None else None,
        }
        for plan, retailer, meal_count_value, known, estimated in rows
    ]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
@router.post(
    "/generate",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(verify_csrf),
        Depends(rate_limit(plan_generation_rate_limiter)),
    ],
)
def generate_plan_endpoint(
    payload: GenerateRequest, user: CurrentUser, db: DbSession
) -> dict:
    """Enqueue plan generation. Returns 202 with the run id and its status URL."""
    ctx = get_household_context(payload.household_id, user, db)
    if ctx.role not in ("owner", "editor"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Permisos insuficientes para esta acción"
        )

    # Cloud-mode quota (server-side); no-op in self_hosted mode.
    check_generation_quota(db, household_id=ctx.household.id, user_id=user.id)

    # Resolve the chosen chain (404/422 if invalid). ``retailer_id`` wins; a bare
    # ``store_id`` is accepted for backward compat and resolves the chain it belongs to.
    # Defaults to the household's chain. Prices are aggregated across the whole chain.
    retailer, store = resolve_plan_retailer(
        db, ctx.household, payload.retailer_id, payload.store_id
    )

    meal_plan, run, _job = create_generation(
        db,
        ctx,
        start_date=payload.start_date,
        end_date=payload.end_date,
        budget_amount=payload.budget_amount,
        currency=payload.currency,
        requirements=[r.to_row() for r in payload.requirements],
        retailer=retailer,
        store=store,
        budget_priority=payload.priority,
    )
    record_audit(
        db, action="plan.generate", actor_user_id=user.id,
        household_id=ctx.household.id, entity_type="meal_plan",
        entity_public_id=meal_plan.public_id,
    )
    return {
        "optimization_run_id": str(run.public_id),
        "meal_plan_id": str(meal_plan.public_id),
        "status": run.status,
        "status_url": _status_url(run.public_id),
    }


@router.get("/runs/{optimization_run_id}")
def get_run_status(
    optimization_run_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict:
    """Poll the status of a generation run (queued .. completed/failed/cancelled)."""
    run = resolve_run(db, user.id, optimization_run_id)
    return serialize_run(db, run)


@router.get("/{meal_plan_id}")
def get_plan(meal_plan_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict:
    """Return the full persisted plan (meals, costs, coverage, grocery summary)."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id)
    return serialize_plan(db, meal_plan)


@router.get("/{meal_plan_id}/comparison")
def get_plan_comparison(
    meal_plan_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict:
    """Compare the plan's mandatory basket cost across every user-visible chain.

    Same guard as ``GET /{meal_plan_id}`` (auth + household membership; 404 for non-members).
    Returns per-chain totals/coverage, the cheapest single chain and the optimal cross-chain
    split (money as strings). When the plan's household has a geocoded address, each chain and
    the split also carry a travel-cost addition (Fase 2); without an address the response is
    the same pure Phase-1 price comparison.
    """
    meal_plan = resolve_plan(db, user.id, meal_plan_id)
    household = db.get(Household, meal_plan.household_id)
    return compare_plan_with_travel(db, meal_plan, get_settings(), household)


@router.post(
    "/{meal_plan_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_csrf)],
)
def regenerate_plan(
    meal_plan_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict:
    """Regenerate the whole plan with a new seed (async)."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    check_generation_quota(db, household_id=meal_plan.household_id, user_id=user.id)
    run, _job = enqueue_regeneration(db, meal_plan, job_type="regenerate_plan")
    record_audit(
        db, action="plan.regenerate", actor_user_id=user.id,
        household_id=meal_plan.household_id, entity_type="meal_plan",
        entity_public_id=meal_plan.public_id,
    )
    return {
        "optimization_run_id": str(run.public_id),
        "meal_plan_id": str(meal_plan.public_id),
        "status": run.status,
        "status_url": _status_url(run.public_id),
    }


@router.post(
    "/{meal_plan_id}/duplicate",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(verify_csrf),
        Depends(rate_limit(plan_generation_rate_limiter)),
    ],
)
def duplicate_plan(
    meal_plan_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict:
    """Clone a plan's configuration into a NEW plan for the next period (async).

    Same household/budget/chain/meal requirements, dates shifted forward, fresh seed. One tap
    to "plan next week" without re-entering everything — the core weekly-cadence retention hook.
    """
    source = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    check_generation_quota(db, household_id=source.household_id, user_id=user.id)
    household = db.get(Household, source.household_id)
    ctx = get_household_context(household.public_id, user, db)
    meal_plan, run, _job = duplicate_generation(db, ctx, source)
    record_audit(
        db, action="plan.duplicate", actor_user_id=user.id,
        household_id=source.household_id, entity_type="meal_plan",
        entity_public_id=meal_plan.public_id,
    )
    return {
        "optimization_run_id": str(run.public_id),
        "meal_plan_id": str(meal_plan.public_id),
        "status": run.status,
        "status_url": _status_url(run.public_id),
    }


@router.post(
    "/{meal_plan_id}/meals/{planned_meal_id}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_csrf)],
)
def regenerate_meal(
    meal_plan_id: uuid.UUID,
    planned_meal_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Regenerate a single meal slot, keeping the other meals fixed (async)."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    check_generation_quota(db, household_id=meal_plan.household_id, user_id=user.id)
    planned_meal = db.execute(
        select(PlannedMeal).where(
            PlannedMeal.public_id == planned_meal_id,
            PlannedMeal.meal_plan_id == meal_plan.id,
        )
    ).scalar_one_or_none()
    if planned_meal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Comida no encontrada")

    payload = build_regenerate_meal_payload(db, meal_plan, planned_meal)
    run, _job = enqueue_regeneration(
        db, meal_plan, job_type="regenerate_meal", payload=payload
    )
    return {
        "optimization_run_id": str(run.public_id),
        "meal_plan_id": str(meal_plan.public_id),
        "planned_meal_id": str(planned_meal.public_id),
        "status": run.status,
        "status_url": _status_url(run.public_id),
    }


# --------------------------------------------------------------------------- #
# Favorites / feedback (feed future generations)
# --------------------------------------------------------------------------- #
@router.post(
    "/recipes/{recipe_id}/favorite",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def add_favorite(
    recipe_id: uuid.UUID, ctx: HouseholdCtx, user: CurrentUser, db: DbSession
) -> dict:
    """Mark a recipe as a household favorite (idempotent). Needs ``?household_id=``."""
    recipe = _resolve_recipe(db, recipe_id)
    existing = db.execute(
        select(FavoriteRecipe).where(
            FavoriteRecipe.household_id == ctx.household.id,
            FavoriteRecipe.recipe_id == recipe.id,
            FavoriteRecipe.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            FavoriteRecipe(
                household_id=ctx.household.id, user_id=user.id, recipe_id=recipe.id
            )
        )
        db.flush()
    return {"recipe_id": str(recipe.public_id), "favorite": True}


@router.delete(
    "/recipes/{recipe_id}/favorite",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_csrf)],
)
def remove_favorite(
    recipe_id: uuid.UUID, ctx: HouseholdCtx, user: CurrentUser, db: DbSession
) -> None:
    """Remove a household favorite. Needs ``?household_id=``."""
    recipe = _resolve_recipe(db, recipe_id)
    existing = db.execute(
        select(FavoriteRecipe).where(
            FavoriteRecipe.household_id == ctx.household.id,
            FavoriteRecipe.recipe_id == recipe.id,
            FavoriteRecipe.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.flush()


@router.post(
    "/recipes/{recipe_id}/feedback", dependencies=[Depends(verify_csrf)]
)
def submit_feedback(
    recipe_id: uuid.UUID,
    payload: FeedbackRequest,
    ctx: HouseholdCtx,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Record like/reject/no_show feedback (upsert). Needs ``?household_id=``."""
    recipe = _resolve_recipe(db, recipe_id)
    existing = db.execute(
        select(RecipeFeedback).where(
            RecipeFeedback.household_id == ctx.household.id,
            RecipeFeedback.recipe_id == recipe.id,
            RecipeFeedback.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            RecipeFeedback(
                household_id=ctx.household.id,
                user_id=user.id,
                recipe_id=recipe.id,
                sentiment=payload.sentiment,
            )
        )
    else:
        existing.sentiment = payload.sentiment
    db.flush()
    return {"recipe_id": str(recipe.public_id), "sentiment": payload.sentiment}


@router.delete(
    "/recipes/{recipe_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_csrf)],
)
def clear_feedback(
    recipe_id: uuid.UUID, ctx: HouseholdCtx, user: CurrentUser, db: DbSession
) -> None:
    """Clear a household member's feedback on a recipe. Needs ``?household_id=``."""
    recipe = _resolve_recipe(db, recipe_id)
    existing = db.execute(
        select(RecipeFeedback).where(
            RecipeFeedback.household_id == ctx.household.id,
            RecipeFeedback.recipe_id == recipe.id,
            RecipeFeedback.user_id == user.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.flush()


@router.get("/recipes/favorites")
def list_favorites(ctx: HouseholdCtx, user: CurrentUser, db: DbSession) -> list[dict]:
    """List the household's favorite recipes, newest first. Needs ``?household_id=``."""
    rows = db.execute(
        select(FavoriteRecipe, Recipe)
        .join(Recipe, Recipe.id == FavoriteRecipe.recipe_id)
        .where(FavoriteRecipe.household_id == ctx.household.id)
        .order_by(FavoriteRecipe.created_at.desc())
    ).all()
    return [
        {**_serialize_recipe_brief(recipe), "favorited_at": favorite.created_at}
        for favorite, recipe in rows
    ]


@router.get("/recipes/feedback")
def list_feedback(
    ctx: HouseholdCtx,
    user: CurrentUser,
    db: DbSession,
    sentiment: FeedbackSentiment | None = None,
) -> list[dict]:
    """List the household's recipe feedback, newest first, optionally filtered by
    ``sentiment`` (``like``/``reject``/``no_show``). Needs ``?household_id=``."""
    query = (
        select(RecipeFeedback, Recipe)
        .join(Recipe, Recipe.id == RecipeFeedback.recipe_id)
        .where(RecipeFeedback.household_id == ctx.household.id)
    )
    if sentiment is not None:
        query = query.where(RecipeFeedback.sentiment == sentiment)
    query = query.order_by(RecipeFeedback.updated_at.desc())
    rows = db.execute(query).all()
    return [
        {
            **_serialize_recipe_brief(recipe),
            "sentiment": feedback.sentiment,
            "updated_at": feedback.updated_at,
        }
        for feedback, recipe in rows
    ]
