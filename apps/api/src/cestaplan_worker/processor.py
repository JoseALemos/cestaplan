"""Core job processing, independent of the polling loop (unit-testable).

``process_job`` runs one :class:`GenerationJob`: it transitions the run through its
lifecycle, builds the engine input via the DB adapter, runs the deterministic engine
and persists the outcome. A feasible plan is materialized; an infeasible one is
recorded as a diagnosis and the run is marked ``failed`` (never a fake plan); an
unexpected exception rolls back this job's work and reschedules with backoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.models import GenerationJob, MealPlan, OptimizationRun
from cestaplan_api.services.plan_service import persist_infeasible, persist_plan_result
from cestaplan_api.services.planning_context import build_plan_input
from cestaplan_engine import PlanResult, generate_plan

_MAX_BACKOFF_SECONDS = 300

# Map the queued job type to the UsageLedger operation tag for any OpenAI call it makes.
_OPERATION_BY_JOB_TYPE = {
    "generate": "plan_generation",
    "regenerate_plan": "plan_regeneration",
    "regenerate_meal": "recipe_regeneration",
}


def _now() -> datetime:
    return datetime.now(UTC)


def process_job(job: GenerationJob, db: Session) -> None:
    """Process a single generation job to completion, failure or reschedule."""
    run = db.get(OptimizationRun, job.optimization_run_id)
    meal_plan = db.get(MealPlan, run.meal_plan_id) if run is not None else None
    if run is None or meal_plan is None:
        job.status = "failed"
        job.last_error = "job references a missing run or plan"
        db.flush()
        return

    try:
        with db.begin_nested():
            _execute(db, job, run, meal_plan)
    except Exception as exc:
        _handle_failure(db, job, run, meal_plan, exc)
    db.flush()


def _execute(
    db: Session, job: GenerationJob, run: OptimizationRun, meal_plan: MealPlan
) -> None:
    _transition(run, job, "collecting_data", started=True)
    provider_warnings: list[str] = []
    plan_input = build_plan_input(
        db,
        meal_plan,
        seed=run.seed,
        warnings=provider_warnings,
        operation=_OPERATION_BY_JOB_TYPE.get(job.job_type, "plan_generation"),
        optimization_run_id=run.id,
    )
    _apply_bias(plan_input, job.payload)

    _transition(run, job, "generating_candidates")
    _transition(run, job, "validating")
    _transition(run, job, "optimizing")

    result = generate_plan(plan_input)
    if isinstance(result, PlanResult):
        if provider_warnings:
            result.warnings = [*provider_warnings, *result.warnings]
        persist_plan_result(db, meal_plan, run, result)
        job.status = "completed"
        job.last_error = None
    else:
        message = persist_infeasible(db, meal_plan, run, result)
        job.status = "failed"
        job.last_error = message
    job.heartbeat_at = _now()


def _transition(
    run: OptimizationRun, job: GenerationJob, new_status: str, *, started: bool = False
) -> None:
    run.status = new_status
    job.status = new_status
    now = _now()
    job.heartbeat_at = now
    if started and run.started_at is None:
        run.started_at = now


def _apply_bias(plan_input, payload: dict[str, Any] | None) -> None:
    """Fold favorite/rejected biasing from the job payload into the engine input."""
    if not payload:
        return
    favorites = {str(r) for r in (payload.get("favorite_recipe_ids") or [])}
    rejected = {str(r) for r in (payload.get("rejected_recipe_ids") or [])}
    if favorites:
        plan_input.favorites = set(plan_input.favorites) | favorites
    if rejected:
        for member in plan_input.members:
            member.rejected_recipe_ids = set(member.rejected_recipe_ids) | rejected


def _handle_failure(
    db: Session,
    job: GenerationJob,
    run: OptimizationRun,
    meal_plan: MealPlan,
    exc: Exception,
) -> None:
    job.attempts += 1
    job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = _now()

    if job.attempts >= job.max_attempts:
        job.status = "failed"
        run.status = "failed"
        run.finished_at = _now()
        meal_plan.status = "failed"
    else:
        job.status = "queued"
        run.status = "queued"
        job.run_after = _now() + _backoff(job.attempts)


def _backoff(attempts: int) -> timedelta:
    base = get_settings().worker_poll_interval_seconds
    seconds = min(_MAX_BACKOFF_SECONDS, base * (2 ** attempts))
    return timedelta(seconds=seconds)
