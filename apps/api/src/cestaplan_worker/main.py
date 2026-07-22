"""CestaPlan job-queue worker loop.

Run with::

    python -m cestaplan_worker.main

Polls :class:`GenerationJob` with ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple
workers never grab the same job. A claimed job is marked ``collecting_data`` +
locked (``locked_by`` / ``locked_at`` / ``heartbeat_at``) before the row lock is
released, then processed by :func:`process_job`.
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import GenerationJob
from cestaplan_worker.processor import process_job


def _now() -> datetime:
    return datetime.now(UTC)


def claim_job(
    db: Session, worker_id: str, now: datetime | None = None
) -> GenerationJob | None:
    """Atomically claim the next runnable job (FOR UPDATE SKIP LOCKED).

    Sets the lock fields and moves the job out of ``queued`` so it is not re-claimed
    once the row lock is released. The caller controls the surrounding transaction.
    """
    now = now or _now()
    job = db.execute(
        select(GenerationJob)
        .where(
            GenerationJob.status == "queued",
            or_(GenerationJob.run_after.is_(None), GenerationJob.run_after <= now),
        )
        .order_by(GenerationJob.priority.desc(), GenerationJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalars().first()
    if job is None:
        return None

    job.status = "collecting_data"
    job.locked_at = now
    job.locked_by = worker_id
    job.heartbeat_at = now
    db.flush()
    return job


def run_worker(
    worker_id: str | None = None,
    *,
    stop: object | None = None,
    max_idle_loops: int | None = None,
) -> None:
    """Poll and process jobs until ``stop`` is set (or ``max_idle_loops`` idle polls)."""
    settings = get_settings()
    worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    interval = settings.worker_poll_interval_seconds
    idle = 0

    def _should_stop() -> bool:
        return bool(stop) and bool(getattr(stop, "is_set", lambda: False)())

    while not _should_stop():
        processed = _poll_once(worker_id)
        if processed:
            idle = 0
            continue
        idle += 1
        if max_idle_loops is not None and idle >= max_idle_loops:
            return
        time.sleep(interval)


def _poll_once(worker_id: str) -> bool:
    """Claim and process at most one job. Returns True if a job was processed."""
    db = SessionLocal()
    try:
        job = claim_job(db, worker_id)
        if job is None:
            db.commit()
            return False
        # Release the row lock; the job is now non-queued so no one re-claims it.
        db.commit()
        process_job(job, db)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    stop = _StopFlag()
    signal.signal(signal.SIGINT, stop.set_from_signal)
    signal.signal(signal.SIGTERM, stop.set_from_signal)
    run_worker(stop=stop)


class _StopFlag:
    def __init__(self) -> None:
        self._stop = False

    def is_set(self) -> bool:
        return self._stop

    def __bool__(self) -> bool:  # allow `bool(stop)` to reflect truthiness of existence
        return True

    def set_from_signal(self, *_: object) -> None:
        self._stop = True


if __name__ == "__main__":
    main()
