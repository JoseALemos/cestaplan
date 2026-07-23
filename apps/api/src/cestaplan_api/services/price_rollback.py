"""Logical rollback of a price sync (spec §T).

A sync is reversible without ever deleting evidence: the observations it created are marked
``rolled_back_at``/``rolled_back_by`` and closed, and any prior observation it had closed is
re-opened — restoring the previous current-price projection. Idempotent: re-running does
nothing unless ``force`` is set. It never mass-DELETEs and never touches other runs' history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import CrawlRun, PriceObservation


@dataclass(slots=True)
class RollbackReport:
    run_id: str
    reopened: int = 0
    invalidated: int = 0
    already_rolled_back: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "reopened": self.reopened,
            "invalidated": self.invalidated,
            "already_rolled_back": self.already_rolled_back,
        }


def rollback_sync(
    db: Session,
    run_public_id: uuid.UUID,
    actor_user_id: int | None,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> RollbackReport:
    """Reverse the effects of one sync run, restoring the prior projection. Idempotent."""
    now = now or datetime.now(UTC)
    run = db.execute(
        select(CrawlRun).where(CrawlRun.public_id == run_public_id)
    ).scalar_one_or_none()
    if run is None:
        raise ValueError(f"unknown run {run_public_id}")

    created = list(
        db.execute(
            select(PriceObservation).where(PriceObservation.crawl_run_id == run.id)
        ).scalars()
    )
    report = RollbackReport(run_id=str(run.public_id))
    if created and all(o.rolled_back_at is not None for o in created) and not force:
        report.already_rolled_back = True
        return report

    # Re-open every observation this run had closed (restore the previous current price).
    closed = db.execute(
        select(PriceObservation).where(PriceObservation.closed_by_run_id == run.id)
    ).scalars()
    for obs in closed:
        obs.valid_until = None
        obs.closed_by_run_id = None
        report.reopened += 1

    # Logically invalidate the observations this run created (never a DELETE).
    for obs in created:
        if obs.rolled_back_at is not None and not force:
            continue
        obs.rolled_back_at = now
        obs.rolled_back_by = actor_user_id
        if obs.valid_until is None:
            obs.valid_until = obs.valid_from  # no longer the current price
        report.invalidated += 1

    db.flush()
    return report


__all__ = ["RollbackReport", "rollback_sync"]
