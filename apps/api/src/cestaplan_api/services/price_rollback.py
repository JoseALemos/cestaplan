"""Logical rollback of a price sync (spec §T).

A sync is reversible without ever deleting evidence: the observations it created are marked
``rolled_back_at``/``rolled_back_by`` and closed, and any prior observation it had closed is
re-opened — restoring the previous current-price projection. Idempotent: re-running does
nothing unless ``force`` is set. It never mass-DELETEs and never touches other runs' history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import CrawlRun, PriceObservation, Retailer

# Cap on the number of observations echoed back in a dry-run/apply report.
_SAMPLE_LIMIT = 20


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


@dataclass(slots=True)
class StagingRollbackReport:
    retailer_slug: str
    scope: str | None = None
    only_orphan: bool = False
    applied: bool = False
    matched: int = 0
    invalidated: int = 0
    sample: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "retailer_slug": self.retailer_slug,
            "scope": self.scope,
            "only_orphan": self.only_orphan,
            "applied": self.applied,
            "matched": self.matched,
            "invalidated": self.invalidated,
            "sample": self.sample,
        }


def rollback_staging_observations(
    db: Session,
    *,
    retailer_slug: str,
    scope: str | None = None,
    only_orphan: bool = False,
    apply: bool = False,
    actor_user_id: int | None = None,
    now: datetime | None = None,
) -> StagingRollbackReport:
    """Logically roll back STAGING price observations of one retailer (never a DELETE).

    Reaches the observations the per-run rollback cannot: staging rows that discovery created
    with ``crawl_run_id=None`` (pass ``only_orphan=True`` to target exactly those). The query is
    ALWAYS constrained to ``staging_only is True`` — the hard safeguard that makes it impossible
    to touch productive history — plus ``rolled_back_at is None`` (so it is idempotent). Optional
    ``scope`` filters by ``price_scope``.

    ``apply=False`` (default) is a dry-run: it reads and reports ``matched`` and a ``sample`` but
    writes nothing. ``apply=True`` marks each match ``rolled_back_at``/``rolled_back_by`` and closes
    ``valid_until = valid_until or valid_from`` (reversible), then flushes. Unknown retailer raises
    ``ValueError``.
    """
    now = now or datetime.now(UTC)
    retailer = db.execute(
        select(Retailer).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()
    if retailer is None:
        raise ValueError(f"unknown retailer {retailer_slug!r}")

    # The staging_only filter is not optional: it is the hard safeguard that keeps this tool
    # from ever reaching productive observations. Never build this query without it.
    stmt = (
        select(PriceObservation)
        .where(PriceObservation.retailer_id == retailer.id)
        .where(PriceObservation.staging_only.is_(True))
        .where(PriceObservation.rolled_back_at.is_(None))
        .order_by(PriceObservation.id)
    )
    if scope is not None:
        stmt = stmt.where(PriceObservation.price_scope == scope)
    if only_orphan:
        stmt = stmt.where(PriceObservation.crawl_run_id.is_(None))

    matches = list(db.execute(stmt).scalars())
    report = StagingRollbackReport(
        retailer_slug=retailer_slug,
        scope=scope,
        only_orphan=only_orphan,
        applied=apply,
        matched=len(matches),
        sample=[
            {"id": obs.id, "scope": obs.price_scope, "crawl_run_id": obs.crawl_run_id}
            for obs in matches[:_SAMPLE_LIMIT]
        ],
    )
    if not apply:
        return report

    for obs in matches:
        obs.rolled_back_at = now
        obs.rolled_back_by = actor_user_id
        if obs.valid_until is None:
            obs.valid_until = obs.valid_from  # no longer the current price
        report.invalidated += 1

    db.flush()
    return report


__all__ = [
    "RollbackReport",
    "StagingRollbackReport",
    "rollback_staging_observations",
    "rollback_sync",
]
