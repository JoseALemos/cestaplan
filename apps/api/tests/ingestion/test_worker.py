"""Crawl worker behaviour (live Postgres, no network).

Covers: a raising connector is isolated (its job fails / backs off) while other jobs still
process; ConnectorState is updated on failure and its circuit opens after the threshold;
success resets the circuit; and the pollable loop drains queued jobs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import JobStatus
from cestaplan_api.ingestion import queue as crawl_queue
from cestaplan_api.ingestion.crawl_worker import (
    ConnectorRegistry,
    CrawlWorker,
    JobOutcome,
    echo_handler,
    process_job,
)
from cestaplan_api.models import ConnectorState, CrawlJob, CrawlRun, Retailer


def _make_run(db: Session, slug: str, *, connector_version: str = "1.0.0") -> CrawlRun:
    retailer = Retailer(slug=slug, name="Worker", adapter_key="test", is_synthetic=True)
    db.add(retailer)
    db.flush()
    run = CrawlRun(
        retailer_id=retailer.id,
        run_type="prices",
        status="running",
        connector_version=connector_version,
        parser_version="1.0.0",
    )
    db.add(run)
    db.flush()
    return run


def _boom(db: Session, job: CrawlJob) -> JobOutcome:
    raise RuntimeError("connector down")


def _connector_state(db: Session, retailer_id: int) -> ConnectorState | None:
    return db.execute(
        select(ConnectorState).where(ConnectorState.retailer_id == retailer_id)
    ).scalars().first()


class _NoCloseSession:
    """Wrap a test session so the worker's ``close()`` is a no-op (commits are savepoints)."""

    def __init__(self, session: Session) -> None:
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_session"), name)

    def close(self) -> None:  # keep the shared test session alive across polls
        pass


def test_process_job_isolates_exception(db_session: Session) -> None:
    run = _make_run(db_session, "worker-iso-unit")
    job = crawl_queue.enqueue_job(db_session, crawl_run_id=run.id, job_type="prices")
    outcome = process_job(db_session, job, _boom)
    assert outcome.ok is False
    assert outcome.error is not None
    assert "connector down" in outcome.error


def test_echo_handler_defaults_ok(db_session: Session) -> None:
    run = _make_run(db_session, "worker-echo")
    job = crawl_queue.enqueue_job(db_session, crawl_run_id=run.id, job_type="prices")
    outcome = echo_handler(db_session, job)
    assert outcome.ok is True


def test_failing_job_is_isolated_others_still_process(db_session: Session) -> None:
    run = _make_run(db_session, "worker-iso")
    crawl_queue.enqueue_job(
        db_session, crawl_run_id=run.id, job_type="bad", max_attempts=2
    )
    crawl_queue.enqueue_job(db_session, crawl_run_id=run.id, job_type="good")

    registry = ConnectorRegistry()
    registry.register("bad", _boom)  # "good" falls through to the echo default
    worker = CrawlWorker(registry=registry)

    # Drain the queue one claim at a time (the failing job backs off into the future and
    # is not re-claimed within this pass).
    handled: list[str] = []
    for _ in range(5):
        job = crawl_queue.claim_job(db_session, worker.worker_id)
        if job is None:
            break
        worker._handle_claimed(db_session, job)
        handled.append(job.job_type)

    assert set(handled) == {"bad", "good"}

    good = db_session.execute(
        select(CrawlJob).where(CrawlJob.job_type == "good")
    ).scalars().one()
    bad = db_session.execute(
        select(CrawlJob).where(CrawlJob.job_type == "bad")
    ).scalars().one()

    assert good.status == JobStatus.COMPLETED.value
    # The failing job is rescheduled with backoff (attempts consumed), never blocking "good".
    assert bad.status == JobStatus.QUEUED.value
    assert bad.attempts == 1
    assert bad.available_at > datetime.now(UTC)


def test_connector_circuit_opens_after_threshold(db_session: Session) -> None:
    run = _make_run(db_session, "worker-circuit")
    # max_attempts=1 -> each job fails exactly once (dead-letter) and records one failure.
    for _ in range(3):
        crawl_queue.enqueue_job(
            db_session, crawl_run_id=run.id, job_type="prices", max_attempts=1
        )

    worker = CrawlWorker(registry=_boom, circuit_threshold=3)
    for _ in range(5):
        job = crawl_queue.claim_job(db_session, worker.worker_id)
        if job is None:
            break
        worker._handle_claimed(db_session, job)

    state = _connector_state(db_session, run.retailer_id)
    assert state is not None
    assert state.consecutive_failures >= 3
    assert state.status == "temporarily_blocked"
    assert state.circuit_open_until is not None
    assert state.last_error is not None


def test_connector_success_resets_circuit(db_session: Session) -> None:
    run = _make_run(db_session, "worker-reset")
    # First a failure, then a success on the same connector resets its state.
    crawl_queue.enqueue_job(
        db_session, crawl_run_id=run.id, job_type="prices", max_attempts=1
    )
    crawl_queue.enqueue_job(db_session, crawl_run_id=run.id, job_type="prices")

    registry = ConnectorRegistry()  # echo default => success
    worker_fail = CrawlWorker(registry=_boom, circuit_threshold=1)
    job1 = crawl_queue.claim_job(db_session, "w")
    assert job1 is not None
    worker_fail._handle_claimed(db_session, job1)
    state = _connector_state(db_session, run.retailer_id)
    assert state is not None and state.consecutive_failures == 1

    worker_ok = CrawlWorker(registry=registry)
    job2 = crawl_queue.claim_job(db_session, "w")
    assert job2 is not None
    worker_ok._handle_claimed(db_session, job2)
    db_session.refresh(state)
    assert state.consecutive_failures == 0
    assert state.status == "active"
    assert state.last_success_at is not None


def test_worker_loop_drains_queue(db_session: Session) -> None:
    run = _make_run(db_session, "worker-loop")
    for _ in range(3):
        crawl_queue.enqueue_job(db_session, crawl_run_id=run.id, job_type="prices")

    def factory() -> Session:
        return _NoCloseSession(db_session)  # type: ignore[return-value]

    worker = CrawlWorker(registry=ConnectorRegistry(), session_factory=factory)
    stats = worker.run(stop=None, max_idle_loops=1, recover_on_start=False)

    assert stats.processed == 3
    assert stats.completed == 3
    remaining = db_session.execute(
        select(CrawlJob).where(
            CrawlJob.crawl_run_id == run.id,
            CrawlJob.status == JobStatus.QUEUED.value,
        )
    ).scalars().all()
    assert remaining == []
