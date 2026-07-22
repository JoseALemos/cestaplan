"""Connector + system health reporting for the ingestion pipeline (FASE A, Task 4).

:class:`ConnectorHealthService` fuses the three per-retailer signals into one honest report:

- :class:`ConnectorState` — circuit-breaker state (status, last success, consecutive
  failures, circuit-open-until).
- the latest :class:`CrawlRun` — the most recent execution and its outcome.
- the latest :class:`CoverageSnapshot` — how much of the catalogue is priced and fresh.

:meth:`ConnectorHealthService.system_health` aggregates a DB-reachability probe with a report
per active retailer, so an operator can see at a glance whether ingestion is healthy. Nothing
here touches the network; the DB probe is a trivial ``SELECT 1``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ConnectorState,
    CoverageSnapshot,
    CrawlRun,
    Retailer,
)


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """Fused health of one retailer's connector."""

    retailer_id: int
    status: str | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    circuit_open_until: datetime | None
    last_error: str | None
    last_run_status: str | None
    last_run_at: datetime | None
    coverage_ratio: Decimal | None
    coverage_status: str | None
    fresh_prices: int | None
    coverage_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SystemHealth:
    """Aggregate ingestion health: DB reachability plus per-connector reports."""

    db_ok: bool
    checked_at: datetime
    connectors: list[ConnectorHealth] = field(default_factory=list)


class ConnectorHealthService:
    """Builds connector and system health reports from persisted state."""

    def report(
        self, db: Session, retailer_id: int, *, store_id: int | None = None
    ) -> ConnectorHealth:
        """Fused health report for one retailer (optionally scoped to a store)."""
        state = self._latest_state(db, retailer_id, store_id=store_id)
        run = self._latest_run(db, retailer_id, store_id=store_id)
        coverage = self._latest_coverage(db, retailer_id, store_id=store_id)
        return ConnectorHealth(
            retailer_id=retailer_id,
            status=state.status if state is not None else None,
            last_attempt_at=state.last_attempt_at if state is not None else None,
            last_success_at=state.last_success_at if state is not None else None,
            consecutive_failures=state.consecutive_failures if state is not None else 0,
            circuit_open_until=state.circuit_open_until if state is not None else None,
            last_error=state.last_error if state is not None else None,
            last_run_status=run.status if run is not None else None,
            last_run_at=run.completed_at or run.started_at if run is not None else None,
            coverage_ratio=coverage.coverage_ratio if coverage is not None else None,
            coverage_status=coverage.status if coverage is not None else None,
            fresh_prices=coverage.fresh_prices if coverage is not None else None,
            coverage_observed_at=coverage.observed_at if coverage is not None else None,
        )

    def system_health(self, db: Session, *, as_of: datetime) -> SystemHealth:
        """DB reachability plus a health report per active retailer."""
        db_ok = self._db_ok(db)
        retailer_ids = (
            db.execute(select(Retailer.id).where(Retailer.is_active.is_(True)))
            .scalars()
            .all()
        )
        connectors = [self.report(db, retailer_id) for retailer_id in retailer_ids]
        return SystemHealth(db_ok=db_ok, checked_at=as_of, connectors=connectors)

    # -- internals ------------------------------------------------------------ #

    @staticmethod
    def _db_ok(db: Session) -> bool:
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    def _latest_state(
        self, db: Session, retailer_id: int, *, store_id: int | None
    ) -> ConnectorState | None:
        stmt = (
            select(ConnectorState)
            .where(ConnectorState.retailer_id == retailer_id)
            .order_by(ConnectorState.updated_at.desc(), ConnectorState.id.desc())
            .limit(1)
        )
        if store_id is not None:
            stmt = stmt.where(ConnectorState.store_id == store_id)
        return db.execute(stmt).scalars().first()

    def _latest_run(
        self, db: Session, retailer_id: int, *, store_id: int | None
    ) -> CrawlRun | None:
        stmt = (
            select(CrawlRun)
            .where(CrawlRun.retailer_id == retailer_id)
            .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
            .limit(1)
        )
        if store_id is not None:
            stmt = stmt.where(CrawlRun.store_id == store_id)
        return db.execute(stmt).scalars().first()

    def _latest_coverage(
        self, db: Session, retailer_id: int, *, store_id: int | None
    ) -> CoverageSnapshot | None:
        stmt = (
            select(CoverageSnapshot)
            .where(CoverageSnapshot.retailer_id == retailer_id)
            .order_by(CoverageSnapshot.observed_at.desc(), CoverageSnapshot.id.desc())
            .limit(1)
        )
        if store_id is not None:
            stmt = stmt.where(CoverageSnapshot.store_id == store_id)
        return db.execute(stmt).scalars().first()


__all__ = ["ConnectorHealth", "ConnectorHealthService", "SystemHealth"]
