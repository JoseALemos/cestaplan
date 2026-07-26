"""Two-layer idempotent persistence (spec §3/§4/§10): one economic fact -> many provenance
occurrences. A change to any of the 16 fact-identity fields is a NEW PriceObservation; a new
crawl/parser reporting the SAME fact is a new PriceObservationOccurrence, never a new obs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import CrawlRun, PriceObservation, PriceObservationOccurrence
from cestaplan_api.services import observation_persistence as op
from cestaplan_api.services.observation_persistence import (
    RECORD_PRICE_FACT_WRITER_CONTRACT_VERSION,
    InvalidPriceFactCandidateState,
    MultipleActiveExactFacts,
    OccurrenceProvenance,
    RecordMetrics,
    record_price_fact,
    writer_contract,
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


# --------------------------------------------------------------------------- #
# Rolled-back facts are never reused (fix/record-price-fact-rolled-back-selection §2-§6)
# --------------------------------------------------------------------------- #
def _insert_obs(db, rid, vid, *, amount, observed_at=T0, valid_until=None, rolled_back=False,
                status="unverified"):
    """Commit-free direct insert to build a specific active/rolled-back/disputed lane state.

    The fingerprint fields mirror ``_candidate`` so the row is an EXACT fingerprint match.
    """
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", available=True, promotion_text=None,
        requires_loyalty=False, observed_at=observed_at, imported_at=observed_at,
        valid_from=observed_at, valid_until=valid_until, confidence_score=Decimal("1.0"),
        staging_only=True, verification_status=status,
        rolled_back_at=(observed_at if rolled_back else None))
    db.add(o)
    db.flush()
    return o


def _occ_obs_ids(db, rid) -> list[int]:
    """Observation ids that have an occurrence, SCOPED to this retailer (the test DB holds other,
    pre-committed rows)."""
    return [
        o.price_observation_id
        for o in db.execute(
            select(PriceObservationOccurrence).join(
                PriceObservation,
                PriceObservation.id == PriceObservationOccurrence.price_observation_id,
            ).where(PriceObservation.retailer_id == rid)
        ).scalars()
    ]


# §6.1 rolled-back exact (smaller id) + active canonical (larger id) -> reuse the active.
def test_reuse_active_over_rolled_back_smaller_id(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)  # smaller id
    active = _insert_obs(db_session, retailer.id, v.id, amount="1.19")               # larger id
    m = RecordMetrics()
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0, metrics=m)
    assert res.observation.id == active.id and res.fact_created is False
    assert m.active_exact_fact_reused == 1 and m.rolled_back_exact_matches_ignored == 1
    db_session.refresh(rb)
    assert rb.rolled_back_at is not None  # rolled-back preserved
    ids = _occ_obs_ids(db_session, retailer.id)
    assert active.id in ids and rb.id not in ids  # occurrence on the active fact only


# §6.2 active canonical (smaller id) + rolled-back exact (larger id) -> reuse the active.
def test_reuse_active_over_rolled_back_larger_id(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    active = _insert_obs(db_session, retailer.id, v.id, amount="1.19")               # smaller id
    _insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)      # larger id
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0)
    assert res.observation.id == active.id and res.fact_created is False


# §6.3 only a rolled-back exact -> create a NEW active fact + occurrence on the new; rb unchanged.
def test_only_rolled_back_creates_new_active(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)
    m = RecordMetrics()
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0, metrics=m)
    assert res.fact_created is True and res.observation.id != rb.id
    assert res.observation.rolled_back_at is None
    assert m.new_fact_created_after_rolled_back_match == 1
    db_session.refresh(rb)
    assert rb.rolled_back_at is not None
    ids = _occ_obs_ids(db_session, retailer.id)
    assert ids == [res.observation.id]  # occurrence on the new fact only


# §6.4 several rolled-back exact -> exactly one new active; none receive new occurrences.
def test_multiple_rolled_back_creates_single_new_active(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rbs = [_insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)
           for _ in range(3)]
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0)
    assert res.fact_created is True
    active_rows = db_session.execute(
        select(PriceObservation).where(PriceObservation.retailer_id == retailer.id,
                                       PriceObservation.rolled_back_at.is_(None))).scalars().all()
    assert [r.id for r in active_rows] == [res.observation.id]  # exactly one active
    assert _occ_obs_ids(db_session, retailer.id) == [res.observation.id]  # none on rolled-back
    for rb in rbs:
        db_session.refresh(rb)
        assert rb.rolled_back_at is not None


# §6.5 candidate arriving with rolled_back_at set -> typed error, zero writes.
def test_candidate_rolled_back_state_rejected(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    before = _counts(db_session, retailer.id)
    cand = _candidate(retailer.id, v.id, amount="1.19")
    cand.rolled_back_at = T0
    m = RecordMetrics()
    with pytest.raises(InvalidPriceFactCandidateState):
        record_price_fact(db_session, cand, OccurrenceProvenance(provider_code="x"),
                          imported_at=T0, metrics=m)
    assert _counts(db_session, retailer.id) == before  # nothing written
    assert m.invalid_candidate_state_blocked == 1


# §6.6 several ACTIVE exact -> fail closed (defense in _find_existing_fact; the preflight, which the
# direct inserts bypass, would normally block a repeated-active-timestamp lane even earlier).
def test_multiple_active_exact_fails_closed(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    _insert_obs(db_session, retailer.id, v.id, amount="1.19")
    _insert_obs(db_session, retailer.id, v.id, amount="1.19")  # two active exact facts
    with pytest.raises(MultipleActiveExactFacts) as ei:
        op._find_existing_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                               staging_only=True)
    assert ei.value.count == 2  # not chosen by id
    assert _occ_obs_ids(db_session, retailer.id) == []  # read-only defense wrote nothing


# §6.7 a DISPUTED (but not rolled-back) exact fact is reused, stays disputed, never current.
def test_disputed_not_rolled_back_is_reused(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    disp = _insert_obs(db_session, retailer.id, v.id, amount="1.19", valid_until=T0,
                       status="disputed")  # empty [T0,T0] barrier
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0)
    assert res.observation.id == disp.id and res.fact_created is False
    assert res.observation.verification_status == "disputed"
    assert res.observation.valid_from == res.observation.valid_until  # still an empty barrier


# §6.8 a DIFFERENT fingerprint still creates a new fact via the normal temporal logic.
def test_different_fingerprint_still_new_fact(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)
    m = RecordMetrics()
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="2.49"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0, metrics=m)
    assert res.fact_created is True and res.observation.id != rb.id
    assert m.rolled_back_exact_matches_ignored == 0  # a different fact, not an ignored match


# §8 behavioral guard: removing the rolled_back filter would resurrect the rolled-back fact here.
def test_behavioral_guard_rolled_back_never_reused(db_session: Session) -> None:
    retailer, v = _fixture(db_session)
    rb = _insert_obs(db_session, retailer.id, v.id, amount="1.19", rolled_back=True)
    res = record_price_fact(db_session, _candidate(retailer.id, v.id, amount="1.19"),
                            OccurrenceProvenance(provider_code="x"), imported_at=T0)
    assert res.observation.id != rb.id and res.observation.rolled_back_at is None
    assert _occ_obs_ids(db_session, retailer.id) == [res.observation.id]


# §4 the versioned writer contract declares the active-only guarantees (evidence for future apply).
def test_writer_contract_declares_active_only() -> None:
    c = writer_contract()
    assert RECORD_PRICE_FACT_WRITER_CONTRACT_VERSION == "record-price-fact-v2-active-only"
    assert c["version"] == RECORD_PRICE_FACT_WRITER_CONTRACT_VERSION
    assert c["exact_fact_reuse_requires_rolled_back_at_null"] is True
    assert c["rolled_back_fact_never_receives_new_occurrence"] is True
    assert c["lane_lock_required"] is True and c["occurrence_lock_required"] is True
    assert c["active_exact_ambiguity_policy"] == "fail_closed"
