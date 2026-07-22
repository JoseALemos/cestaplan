"""End-to-end price-ingestion vertical (live Postgres, no network).

Drives the whole FASE A pipeline through the DemoFixtureConnector + FASE B orchestration and
asserts the invariants that make the subsystem trustworthy:

- RawCaptures are stored (headers redacted, body retained for a fresh fetch).
- PriceObservations are created append-only.
- A price CHANGE across two runs closes the prior ``valid_until`` and inserts a new open row.
- An anomaly (x100 spike) routes to quarantine WITHOUT replacing the last-good open row.
- A CoverageSnapshot reports an honest status.
- The ProductPrice projection is populated so the meal engine can read current prices.
- The CrawlWorker dispatches a demo job through the registry to the orchestration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import JobStatus, RunStatus, RunType
from cestaplan_api.ingestion.connectors.demo import (
    SCENARIO_ANOMALY,
    SCENARIO_BASELINE,
    SCENARIO_PRICE_CHANGE,
    DemoFixtureConnector,
)
from cestaplan_api.ingestion.connectors.registry import build_worker_registry
from cestaplan_api.ingestion.crawl_worker import CrawlWorker
from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.ingestion.orchestration import run_price_sync
from cestaplan_api.ingestion.run_service import CrawlRunService
from cestaplan_api.models import (
    CoverageSnapshot,
    CrawlJob,
    ExternalProduct,
    PriceAnomaly,
    PriceObservation,
    ProductPrice,
    ProductVariant,
    RawCapture,
    Retailer,
    Store,
)

_MUTATED = "DFM-0001"


class _NoCloseSession:
    """Wrap the test session so the worker's ``close()`` is a no-op (commits are savepoints)."""

    def __init__(self, session: Session) -> None:
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_session"), name)

    def close(self) -> None:
        pass


def _seed_retailer_store(db: Session) -> tuple[Retailer, Store]:
    retailer = Retailer(
        slug=DemoFixtureConnector.retailer_code,
        name="DemoFixtureMart",
        adapter_key="demo",
        is_synthetic=True,
    )
    db.add(retailer)
    db.flush()
    store = Store(retailer_id=retailer.id, name="Centro", is_synthetic=True)
    db.add(store)
    db.flush()
    return retailer, store


def _variant(db: Session, retailer_id: int, external_id: str) -> ProductVariant:
    return db.execute(
        select(ProductVariant)
        .join(ExternalProduct, ProductVariant.external_product_id == ExternalProduct.id)
        .where(
            ProductVariant.retailer_id == retailer_id,
            ExternalProduct.external_id == external_id,
        )
    ).scalars().one()


def _obs_count(db: Session, retailer_id: int) -> int:
    return db.execute(
        select(func.count())
        .select_from(PriceObservation)
        .where(PriceObservation.retailer_id == retailer_id)
    ).scalar_one()


def test_baseline_run_populates_captures_observations_coverage_projection(
    db_session: Session,
) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC) - timedelta(minutes=5)

    result = run_price_sync(
        db_session, retailer, store, DemoFixtureConnector(scenario=SCENARIO_BASELINE),
        as_of=as_of,
    )

    assert result.discovered == 26
    assert result.accepted == 26
    assert result.quarantined == 0

    # RawCaptures stored, headers redacted (a dict), body retained for the fresh fetch.
    captures = db_session.execute(
        select(RawCapture).where(RawCapture.retailer_id == retailer.id)
    ).scalars().all()
    assert len(captures) == 26
    assert all(isinstance(c.response_headers, dict) for c in captures)
    assert all(c.is_block_page is False for c in captures)
    assert all(c.body_hash for c in captures)

    # PriceObservations created, append-only (all currently open).
    observations = db_session.execute(
        select(PriceObservation).where(PriceObservation.retailer_id == retailer.id)
    ).scalars().all()
    assert len(observations) == 26
    assert all(o.valid_until is None for o in observations)

    # CoverageSnapshot with an honest status: everything discovered is priced -> complete.
    snapshot = db_session.execute(
        select(CoverageSnapshot).where(CoverageSnapshot.retailer_id == retailer.id)
    ).scalars().one()
    assert snapshot.discovered_products == 26
    assert snapshot.priced_products == 26
    assert snapshot.status == "complete"

    # ProductPrice projection populated so the meal engine can read current prices.
    projected = db_session.execute(
        select(func.count())
        .select_from(ProductPrice)
        .where(ProductPrice.retailer_id == retailer.id)
    ).scalar_one()
    assert projected == 26
    assert result.projected == 26


def test_price_change_closes_interval_and_appends_new_open_row(
    db_session: Session,
) -> None:
    retailer, store = _seed_retailer_store(db_session)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = datetime.now(UTC) - timedelta(hours=1)

    run_price_sync(db_session, retailer, store,
                   DemoFixtureConnector(scenario=SCENARIO_BASELINE), as_of=t0)
    run_price_sync(db_session, retailer, store,
                   DemoFixtureConnector(scenario=SCENARIO_PRICE_CHANGE), as_of=t1)

    variant = _variant(db_session, retailer.id, _MUTATED)
    rows = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.product_variant_id == variant.id)
        .order_by(PriceObservation.valid_from)
    ).scalars().all()

    # History preserved: the prior interval is closed and a fresh open row appended.
    assert len(rows) == 2
    assert rows[0].amount == Decimal("0.89")
    assert rows[0].valid_until == t1
    assert rows[1].amount == Decimal("0.95")
    assert rows[1].valid_until is None

    # Only the changed variant grew history; the rest were revalidated in place.
    assert _obs_count(db_session, retailer.id) == 27


def test_anomaly_routes_to_quarantine_without_replacing_last_good(
    db_session: Session,
) -> None:
    retailer, store = _seed_retailer_store(db_session)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = datetime.now(UTC) - timedelta(hours=1)

    run_price_sync(db_session, retailer, store,
                   DemoFixtureConnector(scenario=SCENARIO_BASELINE), as_of=t0)
    result = run_price_sync(db_session, retailer, store,
                            DemoFixtureConnector(scenario=SCENARIO_ANOMALY), as_of=t1)

    assert result.quarantined == 1
    variant = _variant(db_session, retailer.id, _MUTATED)

    # The last-good open row is untouched (still 0.89), never overwritten by the spike.
    good = db_session.execute(
        select(PriceObservation).where(
            PriceObservation.product_variant_id == variant.id,
            PriceObservation.valid_until.is_(None),
            PriceObservation.verification_status != "disputed",
        )
    ).scalars().one()
    assert good.amount == Decimal("0.89")

    # The current-price read (what the engine sees) returns the last-good price, not the spike.
    current = CurrentPriceService().current(
        db_session, variant.id, store_id=store.id, as_of=t1
    )
    assert current is not None
    assert current.amount == Decimal("0.89")

    # The spike is stored as a disputed row linked to a quarantined anomaly.
    anomaly = db_session.execute(
        select(PriceAnomaly)
        .join(PriceObservation, PriceAnomaly.price_observation_id == PriceObservation.id)
        .where(PriceObservation.product_variant_id == variant.id)
    ).scalars().one()
    assert anomaly.status == "quarantined"
    assert anomaly.anomaly_type == "price_spike"
    assert anomaly.actual_value == Decimal("89.00")


def test_worker_dispatches_demo_job_through_registry(db_session: Session) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC) - timedelta(minutes=1)

    service = CrawlRunService(db_session)
    run = service.create_run(
        retailer_id=retailer.id, store_id=store.id, run_type=RunType.PRICES
    )
    service.enqueue_jobs(run, [_price_job_spec(retailer.slug, store.id)])

    def factory() -> Session:
        return _NoCloseSession(db_session)  # type: ignore[return-value]

    worker = CrawlWorker(
        registry=build_worker_registry(as_of=as_of), session_factory=factory
    )
    stats = worker.run(stop=None, max_idle_loops=1, recover_on_start=False)

    assert stats.processed == 1
    assert stats.completed == 1

    job = db_session.execute(
        select(CrawlJob).where(CrawlJob.crawl_run_id == run.id)
    ).scalars().one()
    assert job.status == JobStatus.COMPLETED.value

    db_session.refresh(run)
    assert run.status == RunStatus.COMPLETED.value
    assert run.accepted_count == 26

    # The dispatched job actually ran the ingestion pipeline end-to-end.
    assert _obs_count(db_session, retailer.id) == 26


def _price_job_spec(retailer_slug: str, store_id: int):
    from cestaplan_api.ingestion.run_service import JobSpec

    return JobSpec(
        job_type=RunType.PRICES.value,
        payload={"retailer_slug": retailer_slug, "store_id": store_id},
    )

