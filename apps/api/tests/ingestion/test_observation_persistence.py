"""Two-layer idempotent persistence (spec §3/§4/§10): one economic fact -> many provenance
occurrences. A change to any of the 16 fact-identity fields is a NEW PriceObservation; a new
crawl/parser reporting the SAME fact is a new PriceObservationOccurrence, never a new obs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import CrawlRun, PriceObservation, PriceObservationOccurrence
from cestaplan_api.services.observation_persistence import (
    OccurrenceProvenance,
    RecordMetrics,
    record_price_fact,
)
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
)

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
PROVIDER = "test_persist_provider"


def _fixture(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "PP-1", name="Persist", price=None)
    return retailer, variant


def _run(db: Session, retailer_id: int) -> int:
    run = CrawlRun(retailer_id=retailer_id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _candidate(retailer_id, variant_id, *, amount, observed_at=T0, promo=None, loyalty=False):
    return PriceObservation(
        retailer_id=retailer_id,
        product_variant_id=variant_id,
        price_scope="national",
        price_type="regular",
        amount=Decimal(amount),
        currency="EUR",
        available=True,
        promotion_text=promo,
        requires_loyalty=loyalty,
        observed_at=observed_at,
        imported_at=T0,
        valid_from=observed_at,
        confidence_score=Decimal("1.0"),
        staging_only=True,
    )


def _counts(db: Session, retailer_id: int) -> tuple[int, int]:
    obs = int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == retailer_id
            )
        )
        or 0
    )
    occ = int(
        db.scalar(
            select(func.count())
            .select_from(PriceObservationOccurrence)
            .join(
                PriceObservation,
                PriceObservation.id == PriceObservationOccurrence.price_observation_id,
            )
            .where(PriceObservation.retailer_id == retailer_id)
        )
        or 0
    )
    return obs, occ


def test_same_fact_two_crawls_one_obs_two_occurrences(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    r1, r2 = _run(db_session, retailer.id), _run(db_session, retailer.id)
    m = RecordMetrics()
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r1), imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r2), imported_at=T1, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (1, 2)
    assert m.observations_created == 1 and m.observations_reused == 1
    assert m.occurrences_created == 2 and m.occurrences_reused == 0


def test_replaying_identical_occurrence_is_idempotent(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    m = RecordMetrics()
    prov = OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=_run(db_session, retailer.id))
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        prov, imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        prov, imported_at=T0, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (1, 1)
    assert m.occurrences_created == 1 and m.occurrences_reused == 1


def test_price_change_is_a_new_fact(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    m = RecordMetrics()
    prov = OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=_run(db_session, retailer.id))
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        prov, imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.29"),
        prov, imported_at=T0, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (2, 2)
    assert m.observations_created == 2


def test_observed_at_change_is_a_new_fact(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    m = RecordMetrics()
    prov = OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=_run(db_session, retailer.id))
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", observed_at=T0),
        prov, imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", observed_at=T1),
        prov, imported_at=T0, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (2, 2)
    assert m.observations_created == 2


def test_promotion_change_is_a_new_fact(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    m = RecordMetrics()
    prov = OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=_run(db_session, retailer.id))
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", promo=None),
        prov, imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", promo="2x1"),
        prov, imported_at=T0, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (2, 2)


def test_loyalty_change_is_a_new_fact(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    m = RecordMetrics()
    prov = OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=_run(db_session, retailer.id))
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", loyalty=False),
        prov, imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", loyalty=True),
        prov, imported_at=T0, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (2, 2)


def test_parser_change_same_fact_is_new_occurrence(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    r1 = _run(db_session, retailer.id)
    m = RecordMetrics()
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r1, parser_version="1.0.0"),
        imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r1, parser_version="2.0.0"),
        imported_at=T0, metrics=m,
    )
    # Same fact, different parser -> 1 observation, 2 occurrences (a re-parse is provenance).
    assert _counts(db_session, retailer.id) == (1, 2)
    assert m.observations_created == 1 and m.observations_reused == 1
    assert m.occurrences_created == 2


def test_value_equal_decimals_are_the_same_fact(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    r1, r2 = _run(db_session, retailer.id), _run(db_session, retailer.id)
    m = RecordMetrics()
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r1), imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.1900"),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r2), imported_at=T1, metrics=m,
    )
    # 1.19 == 1.1900 -> same fact, second call reuses it.
    assert _counts(db_session, retailer.id) == (1, 2)
    assert m.observations_reused == 1


def test_same_fact_different_observed_instant_utc_is_reused(db_session: Session) -> None:
    retailer, variant = _fixture(db_session)
    r1, r2 = _run(db_session, retailer.id), _run(db_session, retailer.id)
    m = RecordMetrics()
    # Same instant in a different timezone offset must be ONE fact (identity normalizes to UTC).
    from datetime import timezone

    madrid = datetime(2026, 7, 25, 10, 0, tzinfo=timezone(timedelta(hours=2)))  # == T0 (08:00 UTC)
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", observed_at=T0),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r1), imported_at=T0, metrics=m,
    )
    record_price_fact(
        db_session, _candidate(retailer.id, variant.id, amount="1.19", observed_at=madrid),
        OccurrenceProvenance(provider_code=PROVIDER, crawl_run_id=r2), imported_at=T1, metrics=m,
    )
    assert _counts(db_session, retailer.id) == (1, 2)
    assert m.observations_reused == 1
