"""Crawl-run orchestration service for the price-ingestion subsystem (FASE A).

:class:`CrawlRunService` owns the lifecycle of a :class:`~cestaplan_api.models.ingestion.CrawlRun`
and the :class:`~cestaplan_api.models.ingestion.CrawlJob` rows it fans out into: creating a
run, enqueuing its jobs (idempotently, via :mod:`cestaplan_api.ingestion.queue`), rolling up
per-job outcome counters, transitioning the run status and computing a coverage score at
completion.

Counters are additive (``record``); the coverage score is derived at completion from the
accepted-vs-discovered ratio. The caller owns the surrounding transaction — these methods
only ``flush``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from cestaplan_api.ingestion import RunStatus, RunType
from cestaplan_api.ingestion.queue import enqueue_job
from cestaplan_api.models import CrawlJob, CrawlRun

_ZERO = Decimal("0")
_ONE = Decimal("1.0000")
_COVERAGE_QUANT = Decimal("0.0001")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A single crawl job to enqueue as part of a run."""

    job_type: str
    payload: dict[str, Any] | None = None
    priority: int = 0
    max_attempts: int = 3
    idempotency_key: str | None = None


class CrawlRunService:
    """Create and drive :class:`CrawlRun` records and their jobs.

    Bound to a session for its lifetime; every mutation flushes but never commits so it
    composes inside the caller's transaction (scheduler, worker, CLI command).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # -- creation -------------------------------------------------------- #
    def create_run(
        self,
        *,
        retailer_id: int,
        run_type: RunType | str,
        store_id: int | None = None,
        scheduled_at: datetime | None = None,
        connector_version: str | None = None,
        parser_version: str | None = None,
        status: RunStatus | str = RunStatus.QUEUED,
    ) -> CrawlRun:
        """Create a queued crawl run for a retailer/store and run type."""
        run = CrawlRun(
            retailer_id=retailer_id,
            store_id=store_id,
            run_type=_value(run_type),
            status=_value(status),
            scheduled_at=scheduled_at or _now(),
            connector_version=connector_version,
            parser_version=parser_version,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def enqueue_jobs(self, run: CrawlRun, specs: Sequence[JobSpec]) -> list[CrawlJob]:
        """Enqueue the given jobs for ``run`` (idempotent per job idempotency key)."""
        jobs: list[CrawlJob] = []
        for spec in specs:
            jobs.append(
                enqueue_job(
                    self.db,
                    crawl_run_id=run.id,
                    job_type=spec.job_type,
                    payload=spec.payload,
                    priority=spec.priority,
                    max_attempts=spec.max_attempts,
                    idempotency_key=spec.idempotency_key,
                )
            )
        return jobs

    # -- counters -------------------------------------------------------- #
    def record(
        self,
        run: CrawlRun,
        *,
        discovered: int = 0,
        fetched: int = 0,
        parsed: int = 0,
        accepted: int = 0,
        rejected: int = 0,
        quarantined: int = 0,
        errors: int = 0,
    ) -> CrawlRun:
        """Additively roll a job's outcome into the run's counters."""
        run.discovered_count += discovered
        run.fetched_count += fetched
        run.parsed_count += parsed
        run.accepted_count += accepted
        run.rejected_count += rejected
        run.quarantined_count += quarantined
        run.error_count += errors
        self.db.flush()
        return run

    # -- transitions ----------------------------------------------------- #
    def start(self, run: CrawlRun, *, now: datetime | None = None) -> CrawlRun:
        """Move a run to ``running`` and stamp ``started_at`` once."""
        now = now or _now()
        run.status = RunStatus.RUNNING.value
        if run.started_at is None:
            run.started_at = now
        self.db.flush()
        return run

    def complete(self, run: CrawlRun, *, now: datetime | None = None) -> CrawlRun:
        """Finish a run: stamp completion and compute its coverage score."""
        now = now or _now()
        run.status = RunStatus.COMPLETED.value
        run.completed_at = now
        run.coverage_score = self.compute_coverage(run)
        self.db.flush()
        return run

    def fail(self, run: CrawlRun, *, now: datetime | None = None) -> CrawlRun:
        """Mark a run failed and stamp completion."""
        now = now or _now()
        run.status = RunStatus.FAILED.value
        run.completed_at = now
        self.db.flush()
        return run

    def cancel(self, run: CrawlRun, *, now: datetime | None = None) -> CrawlRun:
        """Cancel a run and stamp completion."""
        now = now or _now()
        run.status = RunStatus.CANCELLED.value
        run.completed_at = now
        self.db.flush()
        return run

    # -- coverage -------------------------------------------------------- #
    @staticmethod
    def compute_coverage(run: CrawlRun) -> Decimal | None:
        """Coverage score in ``[0, 1]`` as accepted / discovered, or ``None`` if unknown."""
        if run.discovered_count <= 0:
            return None
        ratio = Decimal(run.accepted_count) / Decimal(run.discovered_count)
        clamped = min(_ONE, max(_ZERO, ratio))
        return clamped.quantize(_COVERAGE_QUANT, rounding=ROUND_HALF_UP)


def _value(enum_or_str: Any) -> str:
    """Return the ``.value`` of a StrEnum member, or the string itself."""
    return getattr(enum_or_str, "value", enum_or_str)


__all__ = ["CrawlRunService", "JobSpec"]
