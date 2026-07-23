"""Per-provider cost/quota accounting (FASE 2+, spec §11).

Enforces daily-run and daily-cost caps for paid providers (Parse.bot, Apify) against the
:class:`~cestaplan_api.models.ingestion.ProviderUsage` ledger, and records usage rows. A
value ``<= 0`` disables that particular cap. ``now`` is passed in (never read from the clock
here) so the daily window and tests stay deterministic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.exceptions import ProviderQuotaExceeded
from cestaplan_api.models import ProviderUsage


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def daily_usage(db: Session, provider: str, now: datetime) -> tuple[int, Decimal]:
    """Return ``(run_count, total_cost)`` for ``provider`` since the start of ``now``'s day."""
    since = _day_start(now)
    run_count = (
        db.scalar(
            select(func.count(ProviderUsage.id)).where(
                ProviderUsage.provider == provider,
                ProviderUsage.started_at >= since,
            )
        )
        or 0
    )
    total_cost = (
        db.scalar(
            select(func.coalesce(func.sum(ProviderUsage.estimated_cost), 0)).where(
                ProviderUsage.provider == provider,
                ProviderUsage.started_at >= since,
            )
        )
        or Decimal("0")
    )
    return run_count, Decimal(total_cost)


def check_quota(
    db: Session,
    provider: str,
    now: datetime,
    *,
    max_daily_runs: int = 0,
    max_daily_cost_eur: float = 0.0,
) -> None:
    """Raise :class:`ProviderQuotaExceeded` if starting another run would breach a daily cap."""
    run_count, total_cost = daily_usage(db, provider, now)
    if max_daily_runs > 0 and run_count >= max_daily_runs:
        raise ProviderQuotaExceeded(
            f"{provider}: daily run cap reached ({run_count}/{max_daily_runs})"
        )
    if max_daily_cost_eur > 0 and total_cost >= Decimal(str(max_daily_cost_eur)):
        raise ProviderQuotaExceeded(
            f"{provider}: daily cost cap reached ({total_cost}/{max_daily_cost_eur} EUR)"
        )


def record_usage(
    db: Session,
    provider: str,
    operation: str,
    *,
    started_at: datetime,
    completed_at: datetime | None = None,
    request_count: int = 0,
    product_count: int = 0,
    estimated_cost: Decimal | None = None,
    currency: str | None = "EUR",
    crawl_run_id: int | None = None,
) -> ProviderUsage:
    """Append a usage row to the ledger. Cost is Decimal; never float."""
    usage = ProviderUsage(
        provider=provider,
        operation=operation,
        request_count=request_count,
        product_count=product_count,
        estimated_cost=estimated_cost,
        currency=currency,
        started_at=started_at,
        completed_at=completed_at,
        crawl_run_id=crawl_run_id,
    )
    db.add(usage)
    db.flush()
    return usage


__all__ = ["check_quota", "daily_usage", "record_usage"]
