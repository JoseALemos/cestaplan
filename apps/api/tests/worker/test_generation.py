"""End-to-end worker tests: process_job, infeasibility, queue safety, re-run."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import (
    GenerationJob,
    Ingredient,
    PlannedMeal,
    RecipeIngredient,
)
from cestaplan_api.services.plan_service import (
    enqueue_regeneration,
    serialize_grocery_list,
    serialize_plan,
)
from cestaplan_worker.main import claim_job
from cestaplan_worker.processor import process_job

from .factory import enqueue_plan, make_household


def _planned_meals(db: Session, meal_plan_id: int) -> list[PlannedMeal]:
    return db.execute(
        select(PlannedMeal).where(PlannedMeal.meal_plan_id == meal_plan_id)
    ).scalars().all()


def _recipe_allergens(db: Session, recipe_id: int) -> set[str]:
    rows = db.execute(
        select(Ingredient.allergen_codes)
        .join(RecipeIngredient, RecipeIngredient.ingredient_id == Ingredient.id)
        .where(RecipeIngredient.recipe_id == recipe_id)
    ).scalars().all()
    allergens: set[str] = set()
    for codes in rows:
        allergens |= set(codes or [])
    return allergens


def test_process_job_produces_valid_plan(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")

    process_job(job, db_session)

    assert job.status == "completed"
    assert run.status == "completed"
    assert meal_plan.status == "ready"

    meals = _planned_meals(db_session, meal_plan.id)
    assert len(meals) == 10  # 2 breakfast + 4 lunch + 1 snack + 3 dinner

    # cost_total is a positive Decimal.
    total = Decimal(run.result_summary["cost_total"]["total"])
    assert total > 0

    # HARD safety: no planned meal contains the household allergen.
    for meal in meals:
        assert "gluten" not in _recipe_allergens(db_session, meal.recipe_id)

    # Grocery list exists and is grouped by category.
    grocery = serialize_grocery_list(db_session, meal_plan)
    assert grocery["categories"]
    assert all(group["items"] for group in grocery["categories"])

    # Coverage was computed.
    assert run.result_summary["coverage"]["status"]

    plan = serialize_plan(db_session, meal_plan)
    assert len(plan["planned_meals"]) == 10
    assert plan["budget_diff"] is not None


def test_impossible_budget_is_infeasible(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    meal_plan, run, job = enqueue_plan(db_session, household, member, budget="0.50")

    process_job(job, db_session)

    assert job.status == "failed"
    assert run.status == "failed"
    assert meal_plan.status == "failed"
    # A structured diagnosis, never a fake plan.
    assert run.infeasibility_report is not None
    assert run.result_summary is None
    assert run.infeasibility_report["status"] == "infeasible"
    assert run.infeasibility_report["minimal_conflict"]
    # No planned meals were persisted.
    assert _planned_meals(db_session, meal_plan.id) == []


def test_regeneration_reruns_and_stays_valid(db_session: Session) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    meal_plan, _run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)
    assert len(_planned_meals(db_session, meal_plan.id)) == 10

    # Regenerate the whole plan with a new seed; prior meals are replaced, not doubled.
    new_run, new_job = enqueue_regeneration(db_session, meal_plan)
    process_job(new_job, db_session)

    assert new_job.status == "completed"
    assert new_run.status == "completed"
    meals = _planned_meals(db_session, meal_plan.id)
    assert len(meals) == 10  # idempotent: exactly one plan's worth of meals
    for meal in meals:
        assert "gluten" not in _recipe_allergens(db_session, meal.recipe_id)


def test_two_workers_do_not_grab_the_same_job() -> None:
    """SELECT ... FOR UPDATE SKIP LOCKED: concurrent claims never collide."""
    # Insert a bare queued job committed so two independent connections can see it.
    setup = Session(bind=engine)
    job = GenerationJob(job_type="generate", status="queued", run_after=datetime.now(UTC))
    setup.add(job)
    setup.commit()
    job_id = job.id
    setup.close()

    conn_a = engine.connect()
    conn_b = engine.connect()
    session_a = Session(bind=conn_a)
    session_b = Session(bind=conn_b)
    try:
        session_a.begin()
        session_b.begin()
        claimed_a = claim_job(session_a, "worker-a")
        claimed_b = claim_job(session_b, "worker-b")

        got = [c for c in (claimed_a, claimed_b) if c is not None]
        assert len(got) == 1
        assert got[0].id == job_id
    finally:
        session_a.rollback()
        session_b.rollback()
        session_a.close()
        session_b.close()
        conn_a.close()
        conn_b.close()
        cleanup = Session(bind=engine)
        cleanup.execute(delete(GenerationJob).where(GenerationJob.id == job_id))
        cleanup.commit()
        cleanup.close()
