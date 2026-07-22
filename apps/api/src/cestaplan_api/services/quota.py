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
from cestaplan_api.models import MealPlan, OptimizationRun, UsageLedger


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
    if settings.deployment_mode != "cloud":
        return

    month_start, day_start = period_bounds()

    daily_limit = settings.cloud_daily_generation_limit
    if daily_limit > 0:
        used_today = count_generations(db, household_id, day_start)
        if used_today >= daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Has alcanzado el límite diario de generaciones ({daily_limit}). "
                    "Inténtalo de nuevo mañana."
                ),
            )

    monthly_limit = settings.cloud_monthly_generation_limit
    if monthly_limit > 0:
        used_month = count_generations(db, household_id, month_start)
        if used_month >= monthly_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Has alcanzado el límite mensual de generaciones ({monthly_limit}). "
                    "Inténtalo de nuevo el próximo mes."
                ),
            )

    token_limit = settings.cloud_monthly_token_limit
    if token_limit > 0:
        input_tokens, output_tokens, _cost = token_totals(db, household_id, month_start)
        if input_tokens + output_tokens >= token_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Has alcanzado el límite mensual de tokens ({token_limit}). "
                    "Inténtalo de nuevo el próximo mes."
                ),
            )
