"""Backfill of Layer B provenance (spec §5/§10): one occurrence per historical PriceObservation
from its OWN metadata — idempotent, non-destructive (zero deletions), preserving every crawl run
and never inventing a provider_code."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    PriceObservation,
    PriceObservationOccurrence,
)
from cestaplan_api.tools import backfill_observation_occurrences as backfill
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
)

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
PROVIDER = "test_backfill_provider"
_ON = PriceObservation.id == PriceObservationOccurrence.price_observation_id


def _run(db: Session, retailer_id: int) -> int:
    run = CrawlRun(retailer_id=retailer_id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _obs(db, rid, vid, *, amount, run_id=None, source_id=None, parser=None, imp=T0):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", observed_at=T0, imported_at=imp, valid_from=T0,
        confidence_score=Decimal("1.0"), staging_only=True, crawl_run_id=run_id,
        source_id=source_id, parser_version=parser,
    )
    db.add(o)
    db.flush()
    return o


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "BF-1", name="Backfill", price=None)
    return retailer, variant


def _occ_count(db, rid) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PriceObservationOccurrence)
            .join(PriceObservation, _ON)
            .where(PriceObservation.retailer_id == rid)
        )
        or 0
    )


def _obs_count(db, rid) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == rid
            )
        )
        or 0
    )


def test_dry_run_writes_nothing(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    _obs(db_session, retailer.id, variant.id, amount="1.19", run_id=_run(db_session, retailer.id))
    r = backfill.backfill(db_session, PROVIDER, apply=False)
    assert r["observations_scanned"] == 1
    assert r["occurrences_created"] == 1
    assert r["deletions"] == 0
    assert _occ_count(db_session, retailer.id) == 0  # dry-run persisted nothing


def test_apply_creates_one_occurrence_per_observation_preserving_runs(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    run_a, run_b = _run(db_session, retailer.id), _run(db_session, retailer.id)
    _obs(db_session, retailer.id, variant.id, amount="1.19", run_id=run_a)
    _obs(db_session, retailer.id, variant.id, amount="1.29", run_id=run_b)
    obs_before = _obs_count(db_session, retailer.id)

    r = backfill.backfill(db_session, PROVIDER, apply=True)
    assert r["occurrences_created"] == 2
    assert r["deletions"] == 0
    assert _obs_count(db_session, retailer.id) == obs_before  # zero deletions

    # Every distinct crawl run is preserved in the occurrences (spec §5).
    runs = set(
        db_session.execute(
            select(PriceObservationOccurrence.crawl_run_id)
            .join(PriceObservation, _ON)
            .where(PriceObservation.retailer_id == retailer.id)
        ).scalars()
    )
    assert runs == {run_a, run_b}


def test_backfill_is_idempotent(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    _obs(db_session, retailer.id, variant.id, amount="1.19", run_id=_run(db_session, retailer.id))
    backfill.backfill(db_session, PROVIDER, apply=True)
    first = _occ_count(db_session, retailer.id)

    r2 = backfill.backfill(db_session, PROVIDER, apply=True)
    assert r2["occurrences_created"] == 0
    assert r2["occurrences_already_present"] == 1
    assert _occ_count(db_session, retailer.id) == first  # no duplicate provenance


def test_ambiguous_provenance_is_recorded_but_still_backfilled(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    # No crawl run, no capture, no source -> ambiguous provenance (never invented).
    _obs(db_session, retailer.id, variant.id, amount="1.19", run_id=None, source_id=None)
    r = backfill.backfill(db_session, PROVIDER, apply=True)
    assert r["observations_without_provenance"] == 1
    assert r["ambiguous_provenance"] == 1
    assert r["occurrences_created"] == 1  # still gets a (provenance-less) occurrence
    occ = db_session.execute(
        select(PriceObservationOccurrence).join(
            PriceObservation, PriceObservation.id == PriceObservationOccurrence.price_observation_id
        ).where(PriceObservation.retailer_id == retailer.id)
    ).scalars().one()
    assert occ.provider_code is None  # never invented
    assert occ.crawl_run_id is None and occ.source_id is None
