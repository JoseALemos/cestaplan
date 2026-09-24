"""A2: recover_abandoned_jobs recupera GenerationJob colgados por un worker muerto.

Re-encola los no-terminales con heartbeat vencido (o dead-lettea si agotan intentos), y NO toca
los que tienen heartbeat fresco, ni los queued/terminales.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from cestaplan_api.models import GenerationJob
from cestaplan_worker.main import recover_abandoned_jobs


def _job(
    db: Session, status: str, *, heartbeat_age_min: float | None, attempts: int = 0,
    max_attempts: int = 3,
) -> GenerationJob:
    now = datetime.now(UTC)
    hb = None if heartbeat_age_min is None else now - timedelta(minutes=heartbeat_age_min)
    job = GenerationJob(
        job_type="generate", status=status, attempts=attempts, max_attempts=max_attempts,
        locked_by="dead-worker", locked_at=now - timedelta(minutes=30), heartbeat_at=hb,
    )
    db.add(job)
    db.flush()
    return job


def test_reaper_requeues_stale_in_progress_job(db_session: Session) -> None:
    job = _job(db_session, "optimizing", heartbeat_age_min=30, attempts=0)
    recovered = recover_abandoned_jobs(db_session)
    db_session.refresh(job)
    assert recovered >= 1
    assert job.status == "queued"
    assert job.locked_by is None and job.locked_at is None and job.heartbeat_at is None
    assert job.attempts == 1


def test_reaper_dead_letters_when_attempts_exhausted(db_session: Session) -> None:
    job = _job(db_session, "optimizing", heartbeat_age_min=30, attempts=2, max_attempts=3)
    recover_abandoned_jobs(db_session)
    db_session.refresh(job)
    assert job.status == "failed" and job.attempts == 3
    assert job.last_error is not None and "reaped" in job.last_error


def test_reaper_ignores_fresh_heartbeat(db_session: Session) -> None:
    job = _job(db_session, "optimizing", heartbeat_age_min=0, attempts=0)  # heartbeat ~ now
    recover_abandoned_jobs(db_session)
    db_session.refresh(job)
    assert job.status == "optimizing"  # dentro del timeout: no se toca


def test_reaper_ignores_queued_and_terminal(db_session: Session) -> None:
    queued = _job(db_session, "queued", heartbeat_age_min=30)
    done = _job(db_session, "completed", heartbeat_age_min=30)
    cancelled = _job(db_session, "cancelled", heartbeat_age_min=30)
    recover_abandoned_jobs(db_session)
    for j in (queued, done, cancelled):
        db_session.refresh(j)
    assert queued.status == "queued"
    assert done.status == "completed"
    assert cancelled.status == "cancelled"
