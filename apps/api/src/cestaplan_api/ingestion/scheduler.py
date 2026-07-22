"""Crawl scheduler for the price-ingestion subsystem (FASE A).

:class:`CrawlScheduler` turns configured retailers/stores into crawl runs + jobs. Its
entry point :meth:`schedule_daily` is meant to run once per day (Railway cron / a CLI
command): for every active retailer with an active connector and a configured store it
creates — **idempotently** — a :class:`~cestaplan_api.models.ingestion.CrawlRun` per due
run type (discovery / catalog / prices / offers) and enqueues one crawl job for it.

Two independent guards make double-scheduling impossible:

- a **Postgres session-level advisory lock** (``pg_try_advisory_xact_lock``) so two
  scheduler processes cannot interleave, and
- a **per (retailer, store, run_type) freshness check** driven by a small, per-retailer
  :class:`FrequencyConfig` (cadence in days, not hardcoded clock times) — a run type is
  skipped when a run for it already exists inside its frequency window (so running the
  scheduler twice the same day creates the jobs exactly once).

Manual, force-scheduled variants (:meth:`schedule_retailer`, :meth:`schedule_store`) back
the ``sync_retailer`` / ``sync_store`` CLI commands and ignore the freshness check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import ConnectorStatus, RunType
from cestaplan_api.ingestion.run_service import CrawlRunService, JobSpec
from cestaplan_api.models import ConnectorState, CrawlRun, Retailer, Store

# A stable, arbitrary key for the scheduler advisory lock. Two schedulers that both try to
# run pick the same key; only one acquires it per transaction.
SCHEDULER_ADVISORY_LOCK_KEY = 0x43505F5343484431  # "CP_SCHD1"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FrequencyConfig:
    """Per-retailer scheduling policy: which run types run, how often, at what priority.

    ``cadence_days`` maps a run type to its minimum spacing in days (1 = daily). A run type
    absent from ``cadence_days`` uses ``default_cadence_days``. ``run_types`` limits which
    run types are scheduled at all; ``priority`` seeds the enqueued jobs.
    """

    run_types: tuple[RunType, ...] = (
        RunType.DISCOVERY,
        RunType.CATALOG,
        RunType.PRICES,
        RunType.OFFERS,
    )
    cadence_days: dict[RunType, int] = field(
        default_factory=lambda: {
            RunType.DISCOVERY: 7,
            RunType.CATALOG: 3,
            RunType.PRICES: 1,
            RunType.OFFERS: 1,
        }
    )
    default_cadence_days: int = 1
    priority: int = 0
    max_attempts: int = 3

    def cadence_for(self, run_type: RunType) -> int:
        return self.cadence_days.get(run_type, self.default_cadence_days)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Scheduler configuration: a default policy plus per-retailer-slug overrides."""

    default: FrequencyConfig = field(default_factory=FrequencyConfig)
    per_retailer: dict[str, FrequencyConfig] = field(default_factory=dict)
    # Advisory-lock key for the scheduler mutex. Defaults to the global key (one scheduler
    # at a time in production). Tests may set a unique key so concurrent, pooled-connection
    # test transactions never contend on the same lock.
    advisory_lock_key: int = SCHEDULER_ADVISORY_LOCK_KEY

    def for_retailer(self, slug: str) -> FrequencyConfig:
        return self.per_retailer.get(slug, self.default)


@dataclass(slots=True)
class ScheduleReport:
    """Summary of one scheduling pass."""

    acquired_lock: bool = True
    runs_created: int = 0
    jobs_created: int = 0
    skipped_existing: int = 0
    skipped_retailers: int = 0
    run_public_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "acquired_lock": self.acquired_lock,
            "runs_created": self.runs_created,
            "jobs_created": self.jobs_created,
            "skipped_existing": self.skipped_existing,
            "skipped_retailers": self.skipped_retailers,
            "runs": list(self.run_public_ids),
        }


class CrawlScheduler:
    """Create crawl runs + jobs for configured retailers/stores."""

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()

    # -- daily entry point ---------------------------------------------- #
    def schedule_daily(self, db: Session, *, now: datetime | None = None) -> ScheduleReport:
        """Schedule all due run types for every active retailer/store (idempotent)."""
        now = now or _now()
        report = ScheduleReport()
        if not self._acquire_lock(db):
            report.acquired_lock = False
            return report

        for retailer in self._active_retailers(db):
            if self._connector_blocked(db, retailer.id, now):
                report.skipped_retailers += 1
                continue
            cfg = self.config.for_retailer(retailer.slug)
            for store in self._stores_for(db, retailer):
                self._schedule_store(
                    db, retailer, store, cfg, report, now=now, force=False
                )
        return report

    # -- forced (manual) variants --------------------------------------- #
    def schedule_retailer(
        self,
        db: Session,
        retailer: Retailer,
        *,
        force: bool = True,
        now: datetime | None = None,
    ) -> ScheduleReport:
        """Schedule every configured run type for one retailer's stores immediately."""
        now = now or _now()
        report = ScheduleReport()
        cfg = self.config.for_retailer(retailer.slug)
        for store in self._stores_for(db, retailer):
            self._schedule_store(db, retailer, store, cfg, report, now=now, force=force)
        return report

    def schedule_store(
        self,
        db: Session,
        store: Store,
        *,
        force: bool = True,
        now: datetime | None = None,
    ) -> ScheduleReport:
        """Schedule every configured run type for a single store immediately."""
        now = now or _now()
        report = ScheduleReport()
        retailer = db.get(Retailer, store.retailer_id)
        if retailer is None:
            return report
        cfg = self.config.for_retailer(retailer.slug)
        self._schedule_store(db, retailer, store, cfg, report, now=now, force=force)
        return report

    # -- internals ------------------------------------------------------- #
    def _schedule_store(
        self,
        db: Session,
        retailer: Retailer,
        store: Store | None,
        cfg: FrequencyConfig,
        report: ScheduleReport,
        *,
        now: datetime,
        force: bool,
    ) -> None:
        service = CrawlRunService(db)
        store_id = store.id if store is not None else None
        for run_type in cfg.run_types:
            if not force and self._recent_run_exists(
                db, retailer.id, store_id, run_type, cfg.cadence_for(run_type), now
            ):
                report.skipped_existing += 1
                continue

            run = service.create_run(
                retailer_id=retailer.id,
                store_id=store_id,
                run_type=run_type,
                scheduled_at=now,
            )
            spec = JobSpec(
                job_type=run_type.value,
                payload={
                    "retailer_slug": retailer.slug,
                    "store_id": store_id,
                    "run_type": run_type.value,
                },
                priority=cfg.priority,
                max_attempts=cfg.max_attempts,
                idempotency_key=_idempotency_key(
                    retailer.slug, store_id, run_type, now.date()
                ),
            )
            service.enqueue_jobs(run, [spec])
            report.runs_created += 1
            report.jobs_created += 1
            report.run_public_ids.append(str(run.public_id))

    def _recent_run_exists(
        self,
        db: Session,
        retailer_id: int,
        store_id: int | None,
        run_type: RunType,
        cadence_days: int,
        now: datetime,
    ) -> bool:
        """True when a run of this type exists inside the cadence window (idempotency)."""
        cutoff = _start_of_day(now) - timedelta(days=max(0, cadence_days - 1))
        stmt = select(CrawlRun.id).where(
            CrawlRun.retailer_id == retailer_id,
            CrawlRun.run_type == run_type.value,
            CrawlRun.scheduled_at >= cutoff,
        )
        stmt = stmt.where(
            CrawlRun.store_id == store_id
            if store_id is not None
            else CrawlRun.store_id.is_(None)
        )
        return db.execute(stmt.limit(1)).first() is not None

    def _active_retailers(self, db: Session) -> list[Retailer]:
        return list(
            db.execute(
                select(Retailer)
                .where(Retailer.is_active.is_(True))
                .order_by(Retailer.id.asc())
            ).scalars()
        )

    def _stores_for(self, db: Session, retailer: Retailer) -> list[Store | None]:
        """Active stores for a retailer, or ``[None]`` (retailer-wide scope) if it has none."""
        stores: list[Store | None] = list(
            db.execute(
                select(Store)
                .where(Store.retailer_id == retailer.id, Store.is_active.is_(True))
                .order_by(Store.id.asc())
            ).scalars()
        )
        return stores if stores else [None]

    def _connector_blocked(self, db: Session, retailer_id: int, now: datetime) -> bool:
        """True when a connector for the retailer is disabled or its circuit is open."""
        states = list(
            db.execute(
                select(ConnectorState).where(ConnectorState.retailer_id == retailer_id)
            ).scalars()
        )
        if not states:
            # No connector state yet => treat as schedulable (nothing has failed).
            return False
        for state in states:
            blocked_status = state.status in (
                ConnectorStatus.DISABLED.value,
                ConnectorStatus.UNSUPPORTED.value,
            )
            circuit_open = (
                state.circuit_open_until is not None and state.circuit_open_until > now
            )
            if not blocked_status and not circuit_open:
                return False  # at least one usable connector
        return True

    def _acquire_lock(self, db: Session) -> bool:
        """Try to take the transaction-scoped scheduler advisory lock."""
        from sqlalchemy import text

        acquired = db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": self.config.advisory_lock_key},
        ).scalar()
        return bool(acquired)


def _idempotency_key(
    retailer_slug: str, store_id: int | None, run_type: RunType, day: date
) -> str:
    return f"{retailer_slug}:{store_id}:{run_type.value}:{day.isoformat()}"


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "SCHEDULER_ADVISORY_LOCK_KEY",
    "CrawlScheduler",
    "FrequencyConfig",
    "ScheduleReport",
    "SchedulerConfig",
]
