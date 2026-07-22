"""Usage router (prefix ``/api/v1/usage``): the caller's AI usage summary this period.

Everything is aggregated SERVER-SIDE from persisted rows (``OptimizationRun`` counts and
``UsageLedger`` token/cost totals). Money is emitted as strings; ``estimated_cost`` is
``null`` when no price table is configured. The managed API key is never exposed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from cestaplan_api.config import get_settings
from cestaplan_api.deps import DbSession, HouseholdCtx
from cestaplan_api.services.quota import (
    count_generations,
    period_bounds,
    token_totals,
)

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


@router.get("/me")
def get_my_usage(ctx: HouseholdCtx, db: DbSession) -> dict[str, Any]:
    """Return this household's AI usage summary for the current period. Needs ``?household_id=``."""
    settings = get_settings()
    household_id = ctx.household.id
    month_start, day_start = period_bounds()

    input_tokens, output_tokens, estimated_cost = token_totals(
        db, household_id, month_start
    )
    generations_month = count_generations(db, household_id, month_start)
    generations_day = count_generations(db, household_id, day_start)

    data: dict[str, Any] = {
        "period": {
            "month_start": month_start,
            "day_start": day_start,
        },
        "generations": {
            "month": generations_month,
            "day": generations_day,
        },
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
        "estimated_cost": {
            "amount": str(estimated_cost) if estimated_cost is not None else None,
            "currency": ctx.household.currency,
        },
    }
    if settings.deployment_mode == "cloud":
        data["limits"] = {
            "monthly_generation_limit": settings.cloud_monthly_generation_limit,
            "daily_generation_limit": settings.cloud_daily_generation_limit,
            "monthly_token_limit": settings.cloud_monthly_token_limit,
        }
    return data
