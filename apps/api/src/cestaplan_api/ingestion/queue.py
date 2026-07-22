"""Durable crawl-job queue for the price-ingestion subsystem (FASE A).

A thin, dependency-light layer over :class:`~cestaplan_api.models.ingestion.CrawlJob`
that mirrors the plan queue in :mod:`cestaplan_worker.main`: jobs are taken with
``SELECT ... FOR UPDATE SKIP LOCKED`` so no two workers ever grab the same row.

On top of the plain take path this module adds the operational policy the crawl
pipeline needs:

- **retry with exponential backoff + jitter** (``fail_job``) up to ``max_attempts``,
  then a terminal ``dead_letter`` status;
- **stuck-job recovery** (``recover_stuck_jobs``) for rows whose worker died mid-flight
  (stale ``heartbeat_at``);
- **per-retailer / per-domain concurrency limits** so one retailer can never monopolise
  the workers;
- **idempotent enqueue** keyed by an ``idempotency_key`` carried in the job payload;
- **structured logging** with correlation/run/job identifiers and secret redaction.

The caller always owns the surrounding transaction (these helpers only ``flush``); the
worker commits between claim and processing exactly like the plan worker.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import JobStatus
from cestaplan_api.models import CrawlJob, CrawlRun

logger = logging.getLogger("cestaplan.ingestion.queue")

# Backoff policy for failed jobs (seconds). Exponential on the attempt count, capped, with
# additive jitter so a fleet of workers does not retry a whole batch in lockstep.
_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 3600

# Substrings that mark a payload key as sensitive; its value is redacted in logs.
_SECRET_HINTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "credential",
)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Structured logging (secrets redacted)
# --------------------------------------------------------------------------- #
def _redact(value: Any) -> Any:
    """Recursively redact values whose key hints at a secret."""
    if isinstance(value, Mapping):
        return {
            key: ("***" if _is_secret_key(str(key)) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _log(event: str, **fields: Any) -> None:
    """Emit a structured queue event; ``fields`` are redacted before logging."""
    logger.info(event, extra={"ingestion": _redact(fields)})


def _job_fields(job: CrawlJob, **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "crawl_job_id": job.id,
        "crawl_run_id": job.crawl_run_id,
        "job_type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
    }
    fields.update(extra)
    return fields


# --------------------------------------------------------------------------- #
# Enqueue (idempotent)
# --------------------------------------------------------------------------- #
def enqueue_job(
    db: Session,
    *,
    crawl_run_id: int,
    job_type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    available_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> CrawlJob:
    """Insert a :class:`CrawlJob`, skipping duplicates by idempotency key.

    The idempotency key is taken from the explicit argument or ``payload["idempotency_key"]``.
    When a job with that key already exists (any status) it is returned unchanged, so
    re-running a scheduler never double-enqueues the same unit of work.
    """
    payload = dict(payload) if payload else None
    if idempotency_key is None and payload is not None:
        raw_key = payload.get("idempotency_key")
        idempotency_key = str(raw_key) if raw_key is not None else None
    if idempotency_key is not None:
        if payload is None:
            payload = {}
        payload.setdefault("idempotency_key", idempotency_key)
        existing = _find_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            _log(
                "crawl_job.enqueue_skipped_duplicate",
                **_job_fields(existing, idempotency_key=idempotency_key),
            )
            return existing

    job = CrawlJob(
        crawl_run_id=crawl_run_id,
        job_type=job_type,
        payload=payload,
        status=JobStatus.QUEUED.value,
        priority=priority,
        max_attempts=max_attempts,
        available_at=available_at or _now(),
    )
    db.add(job)
    db.flush()
    _log("crawl_job.enqueued", **_job_fields(job, idempotency_key=idempotency_key))
    return job


def _find_by_idempotency_key(db: Session, idempotency_key: str) -> CrawlJob | None:
    return db.execute(
        select(CrawlJob)
        .where(CrawlJob.payload["idempotency_key"].astext == idempotency_key)
        .order_by(CrawlJob.id.asc())
        .limit(1)
    ).scalars().first()


# --------------------------------------------------------------------------- #
# Claim (FOR UPDATE SKIP LOCKED, concurrency-limited)
# --------------------------------------------------------------------------- #
def claim_job(
    db: Session,
    worker_id: str,
    *,
    domain_limits: Mapping[int, int] | None = None,
    now: datetime | None = None,
) -> CrawlJob | None:
    """Atomically claim the next runnable job.

    Selects a ``queued`` job whose ``available_at`` has passed, ordered by ``priority``
    (highest first) then ``available_at`` (oldest first), locking it with
    ``FOR UPDATE OF crawl_job SKIP LOCKED`` so concurrent workers never collide. The row
    is moved to ``locked`` and stamped (``locked_at`` / ``locked_by`` / ``heartbeat_at``)
    before the row lock is released by the caller's commit.

    ``domain_limits`` maps ``retailer_id -> max in-flight jobs``; a retailer already at or
    above its cap is excluded so it cannot monopolise the worker fleet.
    """
    now = now or _now()
    at_capacity = _retailers_at_capacity(db, domain_limits) if domain_limits else set()

    stmt = (
        select(CrawlJob)
        .join(CrawlRun, CrawlJob.crawl_run_id == CrawlRun.id)
        .where(
            CrawlJob.status == JobStatus.QUEUED.value,
            CrawlJob.available_at <= now,
        )
        .order_by(
            CrawlJob.priority.desc(),
            CrawlJob.available_at.asc(),
            CrawlJob.id.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True, of=CrawlJob)
    )
    if at_capacity:
        stmt = stmt.where(CrawlRun.retailer_id.notin_(at_capacity))

    job = db.execute(stmt).scalars().first()
    if job is None:
        return None

    job.status = JobStatus.LOCKED.value
    job.locked_at = now
    job.locked_by = worker_id
    job.heartbeat_at = now
    db.flush()
    _log("crawl_job.claimed", **_job_fields(job, locked_by=worker_id))
    return job


def _retailers_at_capacity(
    db: Session, domain_limits: Mapping[int, int]
) -> set[int]:
    """Return the set of retailer ids that already hold their max in-flight jobs."""
    rows = db.execute(
        select(CrawlRun.retailer_id, func.count(CrawlJob.id))
        .join(CrawlRun, CrawlJob.crawl_run_id == CrawlRun.id)
        .where(CrawlJob.status == JobStatus.LOCKED.value)
        .group_by(CrawlRun.retailer_id)
    ).all()
    at_capacity: set[int] = set()
    for retailer_id, in_flight in rows:
        limit = domain_limits.get(retailer_id)
        if limit is not None and in_flight >= limit:
            at_capacity.add(retailer_id)
    return at_capacity


# --------------------------------------------------------------------------- #
# Lifecycle transitions
# --------------------------------------------------------------------------- #
def heartbeat(db: Session, job: CrawlJob, *, now: datetime | None = None) -> CrawlJob:
    """Refresh a locked job's liveness so ``recover_stuck_jobs`` leaves it alone."""
    job.heartbeat_at = now or _now()
    db.flush()
    return job


def complete_job(db: Session, job: CrawlJob, *, now: datetime | None = None) -> CrawlJob:
    """Mark a job done and clear its lock."""
    now = now or _now()
    job.status = JobStatus.COMPLETED.value
    job.completed_at = now
    job.heartbeat_at = now
    job.locked_at = None
    job.locked_by = None
    job.last_error = None
    db.flush()
    _log("crawl_job.completed", **_job_fields(job))
    return job


def fail_job(
    db: Session,
    job: CrawlJob,
    error: str | Exception | None = None,
    *,
    now: datetime | None = None,
    jitter: bool = True,
) -> CrawlJob:
    """Record a failed attempt: reschedule with backoff, or dead-letter when exhausted.

    Increments ``attempts``. Below ``max_attempts`` the job returns to ``queued`` with
    ``available_at`` pushed out by an exponential backoff (with optional jitter); at or
    above ``max_attempts`` it becomes terminal ``dead_letter``.
    """
    now = now or _now()
    job.attempts += 1
    if error is not None:
        job.last_error = str(error)[:2000]
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = now

    if job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD_LETTER.value
        db.flush()
        _log("crawl_job.dead_letter", **_job_fields(job, error=job.last_error))
    else:
        delay = backoff_delay(job.attempts, jitter=jitter)
        job.status = JobStatus.QUEUED.value
        job.available_at = now + delay
        db.flush()
        _log(
            "crawl_job.rescheduled",
            **_job_fields(job, retry_in_seconds=round(delay.total_seconds(), 3)),
        )
    return job


def cancel_job(db: Session, job: CrawlJob, *, now: datetime | None = None) -> CrawlJob:
    """Cancel a job (terminal) and clear any lock."""
    job.status = JobStatus.CANCELLED.value
    job.locked_at = None
    job.locked_by = None
    job.heartbeat_at = now or _now()
    db.flush()
    _log("crawl_job.cancelled", **_job_fields(job))
    return job


def recover_stuck_jobs(
    db: Session,
    *,
    heartbeat_timeout: timedelta,
    now: datetime | None = None,
) -> int:
    """Re-queue ``locked`` jobs whose worker died (stale or missing heartbeat).

    A job is stuck when its ``heartbeat_at`` is older than ``heartbeat_timeout`` (or was
    never set). Recovered jobs go back to ``queued`` immediately without consuming an
    attempt — recovery is not a failure of the job itself. Returns the number recovered.
    """
    now = now or _now()
    cutoff = now - heartbeat_timeout
    stuck = db.execute(
        select(CrawlJob)
        .where(
            CrawlJob.status == JobStatus.LOCKED.value,
            or_(
                CrawlJob.heartbeat_at.is_(None),
                CrawlJob.heartbeat_at < cutoff,
            ),
        )
        .with_for_update(skip_locked=True)
    ).scalars().all()

    for job in stuck:
        job.status = JobStatus.QUEUED.value
        job.available_at = now
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        _log("crawl_job.recovered", **_job_fields(job))
    if stuck:
        db.flush()
    return len(stuck)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def backoff_delay(
    attempts: int,
    *,
    base: int = _BASE_BACKOFF_SECONDS,
    cap: int = _MAX_BACKOFF_SECONDS,
    jitter: bool = True,
) -> timedelta:
    """Exponential backoff for retry ``attempts`` (1-based), capped, with additive jitter.

    Jitter adds up to one extra ``base`` interval so simultaneous failures spread out
    rather than retrying in lockstep. Pass ``jitter=False`` for deterministic tests.
    """
    exponent = max(0, attempts - 1)
    seconds = float(min(cap, base * (2**exponent)))
    if jitter:
        seconds += random.uniform(0, base)
        seconds = min(seconds, cap + base)
    return timedelta(seconds=seconds)


__all__ = [
    "backoff_delay",
    "cancel_job",
    "claim_job",
    "complete_job",
    "enqueue_job",
    "fail_job",
    "heartbeat",
    "recover_stuck_jobs",
]
