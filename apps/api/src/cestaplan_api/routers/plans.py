"""Plans router (prefix ``/api/v1/plans``): async generation, results, feedback.

Generation is asynchronous: ``POST /generate`` persists the plan + a queued job and
returns 202 with a status URL; the worker runs the deterministic engine. Every route
verifies household membership server-side (no IDOR); money is returned as strings.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from cestaplan_api.deps import (
    CurrentUser,
    DbSession,
    HouseholdCtx,
    get_household_context,
    verify_csrf,
)
from cestaplan_api.models import FavoriteRecipe, PlannedMeal, Recipe, RecipeFeedback
from cestaplan_api.schemas.plan import FeedbackRequest, GenerateRequest
from cestaplan_api.services.audit import record_audit
from cestaplan_api.services.plan_service import (
    build_regenerate_meal_payload,
    create_generation,
    enqueue_regeneration,
    resolve_plan,
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


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
@router.post(
    "/generate", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_csrf)]
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

    meal_plan, run, _job = create_generation(
        db,
        ctx,
        start_date=payload.start_date,
        end_date=payload.end_date,
        budget_amount=payload.budget_amount,
        currency=payload.currency,
        requirements=[r.to_row() for r in payload.requirements],
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
