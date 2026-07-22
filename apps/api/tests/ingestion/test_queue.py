"""Queue behaviour for the crawl-job queue (live Postgres, no network).

Covers: FOR UPDATE SKIP LOCKED (two workers never claim the same job), heartbeat +
stuck-job recovery, exponential-backoff reschedule and dead-lettering, idempotent
enqueue, and per-retailer concurrency limits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.ingestion import JobStatus
from cestaplan_api.ingestion import queue as crawl_queue
from cestaplan_api.models import CrawlJob, CrawlRun, Retailer


def _make_retailer(db: Session, slug: str = "test-queue-retailer") -> Retailer:
    retailer = Retailer(
        slug=slug, name="Queue Test", adapter_key="test", is_synthetic=True
    )
    db.add(retailer)
    db.flush()
    return retailer


def _make_run(db: Session, retailer_id: int) -> CrawlRun:
    run = CrawlRun(retailer_id=retailer_id, run_type="prices", status="queued")
    db.add(run)
    db.flush()
    return run


def _enqueue(db: Session, run_id: int, **kwargs) -> CrawlJob:
    return crawl_queue.enqueue_job(db, crawl_run_id=run_id, job_type="prices", **kwargs)


def test_claim_marks_job_locked(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)

    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None
    assert job.status == JobStatus.LOCKED.value
    assert job.locked_by == "worker-1"
    assert job.locked_at is not None
    assert job.heartbeat_at is not None


def test_claim_respects_available_at(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    future = datetime.now(UTC) + timedelta(hours=1)
    _enqueue(db_session, run.id, available_at=future)

    assert crawl_queue.claim_job(db_session, "worker-1") is None


def test_claim_orders_by_priority(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id, priority=0)
    _enqueue(db_session, run.id, priority=10)

    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None
    assert job.priority == 10


def test_enqueue_is_idempotent_by_key(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    first = _enqueue(db_session, run.id, idempotency_key="abc")
    second = _enqueue(db_session, run.id, idempotency_key="abc")
    assert first.id == second.id

    count = len(
        db_session.query(CrawlJob).filter(CrawlJob.crawl_run_id == run.id).all()
    )
    assert count == 1


def test_heartbeat_updates_timestamp(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)
    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None

    later = datetime.now(UTC) + timedelta(seconds=30)
    crawl_queue.heartbeat(db_session, job, now=later)
    assert job.heartbeat_at == later


def test_recover_stuck_jobs_requeues_stale_heartbeat(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)

    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None
    assert job.status == JobStatus.LOCKED.value
    # Simulate a worker that locked the job then died: its heartbeat goes stale.
    job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=30)
    db_session.flush()

    recovered = crawl_queue.recover_stuck_jobs(
        db_session, heartbeat_timeout=timedelta(minutes=5)
    )
    assert recovered == 1
    db_session.refresh(job)
    assert job.status == JobStatus.QUEUED.value
    assert job.locked_by is None
    assert job.heartbeat_at is None


def test_recover_leaves_fresh_heartbeat_alone(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)
    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None

    recovered = crawl_queue.recover_stuck_jobs(
        db_session, heartbeat_timeout=timedelta(minutes=5)
    )
    assert recovered == 0
    assert job.status == JobStatus.LOCKED.value


def test_complete_job_clears_lock(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)
    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None

    crawl_queue.complete_job(db_session, job)
    assert job.status == JobStatus.COMPLETED.value
    assert job.completed_at is not None
    assert job.locked_by is None


def test_fail_job_backoff_then_dead_letter(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id, max_attempts=3)
    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None

    now = datetime.now(UTC)
    # First failure -> requeued with backoff in the future.
    crawl_queue.fail_job(db_session, job, "boom", now=now, jitter=False)
    assert job.status == JobStatus.QUEUED.value
    assert job.attempts == 1
    assert job.available_at > now
    assert job.last_error == "boom"

    # Second failure -> still requeued, longer backoff.
    crawl_queue.fail_job(db_session, job, "boom", now=now, jitter=False)
    assert job.status == JobStatus.QUEUED.value
    assert job.attempts == 2

    # Third failure -> exhausted -> dead_letter.
    crawl_queue.fail_job(db_session, job, "boom", now=now, jitter=False)
    assert job.status == JobStatus.DEAD_LETTER.value
    assert job.attempts == 3


def test_backoff_is_exponential(db_session: Session) -> None:
    first = crawl_queue.backoff_delay(1, jitter=False)
    second = crawl_queue.backoff_delay(2, jitter=False)
    third = crawl_queue.backoff_delay(3, jitter=False)
    assert second > first
    assert third > second


def test_cancel_job(db_session: Session) -> None:
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)
    job = crawl_queue.claim_job(db_session, "worker-1")
    assert job is not None

    crawl_queue.cancel_job(db_session, job)
    assert job.status == JobStatus.CANCELLED.value
    assert job.locked_by is None


def test_per_retailer_concurrency_limit(db_session: Session) -> None:
    """A retailer at its in-flight limit yields no more jobs until one frees up."""
    retailer = _make_retailer(db_session)
    run = _make_run(db_session, retailer.id)
    _enqueue(db_session, run.id)
    _enqueue(db_session, run.id)
    limits = {retailer.id: 1}

    first = crawl_queue.claim_job(db_session, "worker-1", domain_limits=limits)
    assert first is not None
    # Retailer already has 1 in-flight (== limit) -> second claim is blocked.
    second = crawl_queue.claim_job(db_session, "worker-2", domain_limits=limits)
    assert second is None

    # Completing the first frees capacity -> the second job can now be claimed.
    crawl_queue.complete_job(db_session, first)
    third = crawl_queue.claim_job(db_session, "worker-2", domain_limits=limits)
    assert third is not None


def test_two_workers_do_not_grab_the_same_job() -> None:
    """SELECT ... FOR UPDATE SKIP LOCKED: concurrent claims never collide."""
    setup = Session(bind=engine)
    retailer = Retailer(
        slug="test-queue-skiplock", name="SkipLock", adapter_key="test", is_synthetic=True
    )
    setup.add(retailer)
    setup.flush()
    run = CrawlRun(retailer_id=retailer.id, run_type="prices", status="queued")
    setup.add(run)
    setup.flush()
    job = CrawlJob(
        crawl_run_id=run.id,
        job_type="prices",
        status="queued",
        available_at=datetime.now(UTC),
    )
    setup.add(job)
    setup.commit()
    job_id = job.id
    run_id = run.id
    retailer_id = retailer.id
    setup.close()

    conn_a = engine.connect()
    conn_b = engine.connect()
    session_a = Session(bind=conn_a)
    session_b = Session(bind=conn_b)
    try:
        session_a.begin()
        session_b.begin()
        claimed_a = crawl_queue.claim_job(session_a, "worker-a")
        claimed_b = crawl_queue.claim_job(session_b, "worker-b")

        got = [c for c in (claimed_a, claimed_b) if c is not None]
        assert len(got) == 1
        assert got[0].id == job_id
    finally:
        session_a.rollback()
        session_b.rollback()
        session_a.close()
        session_b.close()
        conn_a.close()
        conn_b.close()
        cleanup = Session(bind=engine)
        cleanup.execute(delete(CrawlJob).where(CrawlJob.id == job_id))
        cleanup.execute(delete(CrawlRun).where(CrawlRun.id == run_id))
        cleanup.execute(delete(Retailer).where(Retailer.id == retailer_id))
        cleanup.commit()
        cleanup.close()
