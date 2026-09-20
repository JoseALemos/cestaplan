"""Cloud-mode generation quotas, computed SERVER-SIDE.

Quotas apply only when ``deployment_mode == "cloud"``. In ``self_hosted`` mode there
are no limits (the check returns immediately). Counts are derived from persisted rows
(``OptimizationRun`` for generations, ``UsageLedger`` for tokens) over the current
calendar period — never from anything a client supplies. The managed API key is never
read or revealed here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.models import Household, MealPlan, OptimizationRun, UsageLedger


def _now() -> datetime:
    return datetime.now(UTC)


def period_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``(month_start, day_start)`` for the current UTC calendar period."""
    now = now or _now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return month_start, day_start


def count_generations(db: Session, household_id: int, since: datetime) -> int:
    """Number of optimization runs for a household since ``since`` (server-side truth)."""
    return int(
        db.execute(
            select(func.count())
            .select_from(OptimizationRun)
            .join(MealPlan, MealPlan.id == OptimizationRun.meal_plan_id)
            .where(
                MealPlan.household_id == household_id,
                OptimizationRun.created_at >= since,
            )
        ).scalar_one()
    )


def count_user_generations(db: Session, user_id: int, since: datetime) -> int:
    """Optimization runs across ALL households a user owns since ``since``.

    Quick-start mints a fresh household on every call, so a per-household cap would never
    bind. Counting by owner makes the cloud limits actually apply to that path.
    """
    return int(
        db.execute(
            select(func.count())
            .select_from(OptimizationRun)
            .join(MealPlan, MealPlan.id == OptimizationRun.meal_plan_id)
            .join(Household, Household.id == MealPlan.household_id)
            .where(
                Household.owner_user_id == user_id,
                OptimizationRun.created_at >= since,
            )
        ).scalar_one()
    )


def token_totals_for_user(
    db: Session, user_id: int, since: datetime
) -> tuple[int, int, Decimal | None]:
    """Token usage across all households a user owns (see :func:`token_totals`)."""
    row = db.execute(
        select(
            func.coalesce(func.sum(UsageLedger.input_tokens), 0),
            func.coalesce(func.sum(UsageLedger.output_tokens), 0),
            func.sum(UsageLedger.estimated_cost),
        )
        .join(Household, Household.id == UsageLedger.household_id)
        .where(Household.owner_user_id == user_id, UsageLedger.created_at >= since)
    ).one()
    input_tokens, output_tokens, estimated_cost = row
    return int(input_tokens), int(output_tokens), estimated_cost


def token_totals(
    db: Session, household_id: int, since: datetime
) -> tuple[int, int, Decimal | None]:
    """Return ``(input_tokens, output_tokens, estimated_cost)`` for the period.

    ``estimated_cost`` is ``None`` when no ledger row in the period carries a cost
    (i.e. no price table configured), so a cost is never fabricated.
    """
    row = db.execute(
        select(
            func.coalesce(func.sum(UsageLedger.input_tokens), 0),
            func.coalesce(func.sum(UsageLedger.output_tokens), 0),
            func.sum(UsageLedger.estimated_cost),
        ).where(
            UsageLedger.household_id == household_id,
            UsageLedger.created_at >= since,
        )
    ).one()
    input_tokens, output_tokens, estimated_cost = row
    return int(input_tokens), int(output_tokens), estimated_cost


def _enforce_quota(db: Session, settings: Settings, *, gen_count, token_count) -> None:
    """Shared cloud-mode enforcement for a generation. ``gen_count(since)`` returns the
    generations in that window; ``token_count(since)`` returns ``(in, out, cost)``. The
    two callables choose the SCOPE (a single household, or all of a user's households) so
    both the per-household and quick-start paths raise identical 429s. No-op off cloud.
    """
    if settings.deployment_mode != "cloud":
        return

    month_start, day_start = period_bounds()

    daily_limit = settings.cloud_daily_generation_limit
    if daily_limit > 0 and gen_count(day_start) >= daily_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Has alcanzado el límite diario de generaciones ({daily_limit}). "
                "Inténtalo de nuevo mañana."
            ),
        )

    monthly_limit = settings.cloud_monthly_generation_limit
    if monthly_limit > 0 and gen_count(month_start) >= monthly_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Has alcanzado el límite mensual de generaciones ({monthly_limit}). "
                "Inténtalo de nuevo el próximo mes."
            ),
        )

    token_limit = settings.cloud_monthly_token_limit
    if token_limit > 0:
        input_tokens, output_tokens, _cost = token_count(month_start)
        if input_tokens + output_tokens >= token_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Has alcanzado el límite mensual de tokens ({token_limit}). "
                    "Inténtalo de nuevo el próximo mes."
                ),
            )


def check_generation_quota(
    db: Session,
    *,
    household_id: int,
    user_id: int | None = None,
    settings: Settings | None = None,
) -> None:
    """Raise HTTP 429 when the household has exhausted its allowed generations.

    No-op unless ``deployment_mode == "cloud"``. Limits <= 0 are treated as disabled.
    """
    settings = settings or get_settings()
    _enforce_quota(
        db,
        settings,
        gen_count=lambda since: count_generations(db, household_id, since),
        token_count=lambda since: token_totals(db, household_id, since),
    )


def check_generation_quota_for_new_household(
    db: Session, *, user_id: int, settings: Settings | None = None
) -> None:
    """Quota for a path that creates a fresh household per call (quick-start).

    A per-household cap never binds there, so counting is scoped to ALL the households the
    user owns. Call this BEFORE creating the household, so an over-quota user is rejected
    without leaving a throwaway household behind. No-op unless ``deployment_mode == "cloud"``.
    """
    settings = settings or get_settings()
    _enforce_quota(
        db,
        settings,
        gen_count=lambda since: count_user_generations(db, user_id, since),
        token_count=lambda since: token_totals_for_user(db, user_id, since),
    )
