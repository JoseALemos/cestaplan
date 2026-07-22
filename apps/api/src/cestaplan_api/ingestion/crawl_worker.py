"""Crawl worker loop for the price-ingestion subsystem (FASE A).

A :class:`CrawlWorker` polls the crawl queue (:mod:`cestaplan_api.ingestion.queue`),
claims one job at a time with ``FOR UPDATE SKIP LOCKED``, dispatches it to a connector
handler and records the outcome — completing the job or failing it with backoff, and
updating the retailer's :class:`~cestaplan_api.models.ingestion.ConnectorState`
circuit-breaker signals.

Connector dispatch is **pluggable**: the worker is given a ``registry`` callable
``(db, job) -> JobOutcome``. The real/demo connectors plug in later; the default here is a
safe echo handler so the loop is fully runnable and testable today. Every job is processed
inside its own ``try`` so **one connector's failure never stops the others** — a raising
handler only fails that job (backoff/dead-letter) and trips its retailer's circuit.

Run it with::

    python -m cestaplan_api.ingestion.crawl_worker
    python -m cestaplan_api.jobs.crawl_worker   # thin CLI wrapper
"""

from __future__ import annotations

import logging
import os
import signal
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion import ConnectorStatus
from cestaplan_api.ingestion import queue as crawl_queue
from cestaplan_api.models import ConnectorState, CrawlJob, CrawlRun

logger = logging.getLogger("cestaplan.ingestion.worker")

# Defaults for the loop / circuit breaker.
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_DEFAULT_HEARTBEAT_TIMEOUT = timedelta(minutes=5)
_DEFAULT_CIRCUIT_THRESHOLD = 5
_DEFAULT_CIRCUIT_COOLDOWN = timedelta(minutes=15)
_UNKNOWN_VERSION = "unknown"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Result of dispatching one crawl job to a connector handler."""

    ok: bool
    retailer_code: str | None = None
    error: str | None = None
    detail: dict[str, Any] | None = None


#: A connector dispatch callback: given the session and a claimed job, do the work and
#: return a :class:`JobOutcome`. It may raise — the worker isolates and records failures.
JobHandler = Callable[[Session, CrawlJob], JobOutcome]


def echo_handler(db: Session, job: CrawlJob) -> JobOutcome:
    """Default no-op handler: acknowledges the job without touching any source."""
    return JobOutcome(ok=True, detail={"echo": job.job_type})


class ConnectorRegistry:
    """Maps a crawl job to its connector handler, with a default fallback.

    Handlers can be registered per ``job_type``; anything unregistered falls back to the
    default (echo). This keeps the demo/real connectors pluggable without the worker
    knowing any concrete connector.
    """

    def __init__(self, default: JobHandler = echo_handler) -> None:
        self._default = default
        self._by_job_type: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._by_job_type[job_type] = handler

    def resolve(self, job: CrawlJob) -> JobHandler:
        return self._by_job_type.get(job.job_type, self._default)

    def __call__(self, db: Session, job: CrawlJob) -> JobOutcome:
        return self.resolve(job)(db, job)


def process_job(db: Session, job: CrawlJob, handler: JobHandler) -> JobOutcome:
    """Dispatch a single job, converting any handler exception into a failed outcome."""
    try:
        outcome = handler(db, job)
    except Exception as exc:  # isolate: this job fails, the loop keeps going
        logger.exception(
            "crawl_job.handler_error",
            extra={"ingestion": {"crawl_job_id": job.id, "job_type": job.job_type}},
        )
        return JobOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
    return outcome


@dataclass(slots=True)
class WorkerStats:
    """Rolling counters for a worker run (useful for tests and CLI summaries)."""

    processed: int = 0
    completed: int = 0
    failed: int = 0
    recovered: int = 0


class CrawlWorker:
    """Polls and processes crawl jobs until stopped."""

    def __init__(
        self,
        *,
        registry: JobHandler | None = None,
        domain_limits: dict[int, int] | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        heartbeat_timeout: timedelta = _DEFAULT_HEARTBEAT_TIMEOUT,
        circuit_threshold: int = _DEFAULT_CIRCUIT_THRESHOLD,
        circuit_cooldown: timedelta = _DEFAULT_CIRCUIT_COOLDOWN,
        session_factory: Callable[[], Session] = SessionLocal,
        worker_id: str | None = None,
    ) -> None:
        self.registry: JobHandler = registry or ConnectorRegistry()
        self.domain_limits = domain_limits
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_timeout = heartbeat_timeout
        self.circuit_threshold = circuit_threshold
        self.circuit_cooldown = circuit_cooldown
        self._session_factory = session_factory
        self.worker_id = worker_id or f"crawl-worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stats = WorkerStats()

    # -- loop ------------------------------------------------------------ #
    def run(
        self,
        *,
        stop: Any | None = None,
        max_idle_loops: int | None = None,
        recover_on_start: bool = True,
    ) -> WorkerStats:
        """Poll and process jobs until ``stop`` is set (or ``max_idle_loops`` idle polls)."""
        if recover_on_start:
            self.stats.recovered += self.recover_stuck()

        idle = 0
        while not _should_stop(stop):
            processed = self.process_next()
            if processed:
                idle = 0
                continue
            idle += 1
            if max_idle_loops is not None and idle >= max_idle_loops:
                break
            time.sleep(self.poll_interval_seconds)
        return self.stats

    def recover_stuck(self) -> int:
        """Re-queue jobs abandoned by a dead worker (called on startup)."""
        db = self._session_factory()
        try:
            count = crawl_queue.recover_stuck_jobs(
                db, heartbeat_timeout=self.heartbeat_timeout
            )
            db.commit()
            return count
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def process_next(self) -> bool:
        """Claim and process at most one job. Returns True if a job was processed."""
        db = self._session_factory()
        try:
            job = crawl_queue.claim_job(
                db, self.worker_id, domain_limits=self.domain_limits
            )
            if job is None:
                db.commit()
                return False
            # Release the row lock: the job is now ``locked`` so no one re-claims it.
            db.commit()
            self._handle_claimed(db, job)
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # -- per-job handling ------------------------------------------------ #
    def _handle_claimed(self, db: Session, job: CrawlJob) -> None:
        crawl_queue.heartbeat(db, job)
        outcome = process_job(db, job, self.registry)
        run = db.get(CrawlRun, job.crawl_run_id)

        if outcome.ok:
            crawl_queue.complete_job(db, job)
            self.stats.completed += 1
            if run is not None:
                self._record_connector_success(db, run)
        else:
            crawl_queue.fail_job(db, job, outcome.error)
            self.stats.failed += 1
            if run is not None:
                self._record_connector_failure(db, run, outcome.error)
        self.stats.processed += 1

    # -- connector state / circuit breaker ------------------------------- #
    def _record_connector_success(self, db: Session, run: CrawlRun) -> None:
        state = self._get_or_create_state(db, run)
        now = _now()
        state.consecutive_failures = 0
        state.last_attempt_at = now
        state.last_success_at = now
        state.last_error = None
        state.circuit_open_until = None
        state.status = ConnectorStatus.ACTIVE.value
        db.flush()

    def _record_connector_failure(
        self, db: Session, run: CrawlRun, error: str | None
    ) -> None:
        state = self._get_or_create_state(db, run)
        now = _now()
        state.consecutive_failures += 1
        state.last_attempt_at = now
        if error is not None:
            state.last_error = error[:2000]
        if state.consecutive_failures >= self.circuit_threshold:
            state.status = ConnectorStatus.TEMPORARILY_BLOCKED.value
            state.circuit_open_until = now + self.circuit_cooldown
            logger.warning(
                "connector.circuit_open",
                extra={
                    "ingestion": {
                        "retailer_id": run.retailer_id,
                        "consecutive_failures": state.consecutive_failures,
                    }
                },
            )
        else:
            state.status = ConnectorStatus.DEGRADED.value
        db.flush()

    def _get_or_create_state(self, db: Session, run: CrawlRun) -> ConnectorState:
        version = run.connector_version or _UNKNOWN_VERSION
        state = db.execute(
            select(ConnectorState).where(
                ConnectorState.retailer_id == run.retailer_id,
                (
                    ConnectorState.store_id == run.store_id
                    if run.store_id is not None
                    else ConnectorState.store_id.is_(None)
                ),
                ConnectorState.connector_version == version,
            )
        ).scalars().first()
        if state is None:
            state = ConnectorState(
                retailer_id=run.retailer_id,
                store_id=run.store_id,
                connector_version=version,
                parser_version=run.parser_version or _UNKNOWN_VERSION,
                status=ConnectorStatus.ACTIVE.value,
                consecutive_failures=0,
            )
            db.add(state)
            db.flush()
        return state


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class StopFlag:
    """Cooperative stop flag toggled by SIGINT/SIGTERM."""

    _stop: bool = field(default=False)

    def is_set(self) -> bool:
        return self._stop

    def __bool__(self) -> bool:
        return True

    def set_from_signal(self, *_: object) -> None:
        self._stop = True


def _should_stop(stop: Any | None) -> bool:
    return bool(stop) and bool(getattr(stop, "is_set", lambda: False)())


def main() -> None:
    """Runnable entry point: poll forever until SIGINT/SIGTERM."""
    logging.basicConfig(level=logging.INFO)
    stop = StopFlag()
    signal.signal(signal.SIGINT, stop.set_from_signal)
    signal.signal(signal.SIGTERM, stop.set_from_signal)
    worker = CrawlWorker()
    logger.info(
        "crawl_worker.start", extra={"ingestion": {"worker_id": worker.worker_id}}
    )
    worker.run(stop=stop)


__all__ = [
    "ConnectorRegistry",
    "CrawlWorker",
    "JobHandler",
    "JobOutcome",
    "StopFlag",
    "WorkerStats",
    "echo_handler",
    "main",
    "process_job",
]


if __name__ == "__main__":
    main()
