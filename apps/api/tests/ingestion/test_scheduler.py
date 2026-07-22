"""Scheduler + run-service behaviour (live Postgres, no network).

Covers: schedule_daily is idempotent (running it twice the same day enqueues jobs once),
forced retailer/store scheduling, the run-service counters + coverage score, and that a
blocked connector is skipped.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import RunType
from cestaplan_api.ingestion.run_service import CrawlRunService
from cestaplan_api.ingestion.scheduler import (
    CrawlScheduler,
    FrequencyConfig,
    SchedulerConfig,
)
from cestaplan_api.models import ConnectorState, CrawlJob, CrawlRun, Retailer, Store

# A daily-cadence config for every run type so "twice the same day" is a clean idempotency
# check for all four run types.
_DAILY = SchedulerConfig(
    default=FrequencyConfig(
        cadence_days=dict.fromkeys(RunType, 1),
        default_cadence_days=1,
    )
)


def _make_retailer_store(db: Session, slug: str) -> tuple[Retailer, Store]:
    retailer = Retailer(slug=slug, name="Sched", adapter_key="test", is_synthetic=True)
    db.add(retailer)
    db.flush()
    store = Store(retailer_id=retailer.id, name="Sched Store", is_synthetic=True)
    db.add(store)
    db.flush()
    return retailer, store


def _job_count(db: Session, retailer_id: int) -> int:
    return db.scalar(
        select(func.count(CrawlJob.id))
        .join(CrawlRun, CrawlJob.crawl_run_id == CrawlRun.id)
        .where(CrawlRun.retailer_id == retailer_id)
    ) or 0


def _run_count(db: Session, retailer_id: int) -> int:
    return db.scalar(
        select(func.count(CrawlRun.id)).where(CrawlRun.retailer_id == retailer_id)
    ) or 0


def test_schedule_daily_creates_runs_and_jobs(db_session: Session) -> None:
    retailer, _store = _make_retailer_store(db_session, "sched-basic")
    scheduler = CrawlScheduler(_DAILY)

    report = scheduler.schedule_daily(db_session)
    assert report.acquired_lock is True
    # Four run types (discovery/catalog/prices/offers) -> four runs + four jobs for our
    # retailer (the pass also schedules any other active retailers in the DB).
    assert _run_count(db_session, retailer.id) == 4
    assert _job_count(db_session, retailer.id) == 4


def test_schedule_daily_is_idempotent(db_session: Session) -> None:
    retailer, _store = _make_retailer_store(db_session, "sched-idem")
    scheduler = CrawlScheduler(_DAILY)

    scheduler.schedule_daily(db_session)
    jobs_after_first = _job_count(db_session, retailer.id)
    runs_after_first = _run_count(db_session, retailer.id)

    second = scheduler.schedule_daily(db_session)
    # Second run of the day creates nothing new.
    assert second.runs_created == 0
    assert second.jobs_created == 0
    assert second.skipped_existing >= 4
    assert _job_count(db_session, retailer.id) == jobs_after_first
    assert _run_count(db_session, retailer.id) == runs_after_first


def test_schedule_retailer_force_ignores_freshness(db_session: Session) -> None:
    retailer, _store = _make_retailer_store(db_session, "sched-force")
    scheduler = CrawlScheduler(_DAILY)

    scheduler.schedule_daily(db_session)
    first = _run_count(db_session, retailer.id)

    forced = scheduler.schedule_retailer(db_session, retailer, force=True)
    assert forced.runs_created == 4
    assert _run_count(db_session, retailer.id) == first + 4


def test_schedule_store_force(db_session: Session) -> None:
    retailer, store = _make_retailer_store(db_session, "sched-store")
    scheduler = CrawlScheduler(_DAILY)

    report = scheduler.schedule_store(db_session, store, force=True)
    assert report.runs_created == 4
    runs = list(
        db_session.execute(
            select(CrawlRun).where(CrawlRun.retailer_id == retailer.id)
        ).scalars()
    )
    assert all(r.store_id == store.id for r in runs)


def test_schedule_daily_skips_blocked_connector(db_session: Session) -> None:
    retailer, store = _make_retailer_store(db_session, "sched-blocked")
    # A disabled connector state blocks scheduling for this retailer.
    db_session.add(
        ConnectorState(
            retailer_id=retailer.id,
            store_id=store.id,
            connector_version="1.0.0",
            parser_version="1.0.0",
            status="disabled",
        )
    )
    db_session.flush()

    report = CrawlScheduler(_DAILY).schedule_daily(db_session)
    assert report.skipped_retailers >= 1
    assert _run_count(db_session, retailer.id) == 0


def test_run_service_counters_and_coverage(db_session: Session) -> None:
    retailer, store = _make_retailer_store(db_session, "runsvc")
    service = CrawlRunService(db_session)

    run = service.create_run(
        retailer_id=retailer.id, store_id=store.id, run_type=RunType.PRICES
    )
    assert run.status == "queued"

    service.start(run)
    assert run.status == "running"
    assert run.started_at is not None

    service.record(run, discovered=10, accepted=8, rejected=2)
    assert run.discovered_count == 10
    assert run.accepted_count == 8

    service.complete(run)
    assert run.status == "completed"
    assert run.completed_at is not None
    # coverage = accepted / discovered = 8 / 10 = 0.8000
    assert run.coverage_score == Decimal("0.8000")


def test_run_service_coverage_none_without_discovery(db_session: Session) -> None:
    retailer, store = _make_retailer_store(db_session, "runsvc-empty")
    service = CrawlRunService(db_session)
    run = service.create_run(
        retailer_id=retailer.id, store_id=store.id, run_type=RunType.HEALTH
    )
    service.complete(run)
    assert run.coverage_score is None


def test_frequency_config_limits_run_types(db_session: Session) -> None:
    retailer, _store = _make_retailer_store(db_session, "sched-freq")
    config = SchedulerConfig(
        default=FrequencyConfig(run_types=(RunType.PRICES,), default_cadence_days=1)
    )
    CrawlScheduler(config).schedule_daily(db_session)
    assert _run_count(db_session, retailer.id) == 1
    runs = list(
        db_session.execute(
            select(CrawlRun).where(CrawlRun.retailer_id == retailer.id)
        ).scalars()
    )
    assert [r.run_type for r in runs] == ["prices"]
