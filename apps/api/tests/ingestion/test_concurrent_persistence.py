"""Real-concurrency proof for record_price_fact (spec §5): independent PostgreSQL connections whose
transactions actually contend on the advisory locks. A threading.Barrier makes every writer hit the
critical section simultaneously, so the fact/occurrence advisory locks — not luck — are what keeps
persistence idempotent and race-free.

These tests COMMIT to the real DB (that is the point — advisory xact locks only contend across real
transactions), so each uses an isolated synthetic retailer and cleans up every committed row.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import (
    CrawlRun,
    ExternalProduct,
    PriceAnomaly,
    PriceObservation,
    PriceObservationOccurrence,
    ProductVariant,
    Retailer,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.services.observation_persistence import (
    OccurrenceProvenance,
    record_price_fact,
)
from cestaplan_api.services.price_history_lane import lane_invariants_hold

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _session() -> Session:
    """A Session on its OWN connection (real concurrency, unlike the shared-savepoint fixture)."""
    return Session(bind=engine.connect(), expire_on_commit=False)


@pytest.fixture()
def seeded():
    """Commit an isolated retailer + variant + two crawl runs; clean everything up afterwards."""
    slug = f"conc-{uuid.uuid4().hex[:10]}"
    s = _session()
    try:
        r = Retailer(slug=slug, name="Concurrency", adapter_key="test", is_synthetic=True)
        s.add(r)
        s.flush()
        ext = ExternalProduct(retailer_id=r.id, external_id="CT-1")
        s.add(ext)
        s.flush()
        v = ProductVariant(
            retailer_id=r.id, external_product_id=ext.id, display_name="V", product_id=None
        )
        s.add(v)
        runs = [CrawlRun(retailer_id=r.id, run_type="prices", status="completed") for _ in range(2)]
        s.add_all(runs)
        s.flush()
        rid, vid, run_ids = r.id, v.id, [runs[0].id, runs[1].id]
        s.commit()
    finally:
        s.close()
    try:
        yield rid, vid, run_ids
    finally:
        c = _session()
        try:
            obs_ids = c.execute(
                select(PriceObservation.id).where(PriceObservation.retailer_id == rid)
            ).scalars().all()
            if obs_ids:
                c.execute(
                    delete(PriceObservationOccurrence).where(
                        PriceObservationOccurrence.price_observation_id.in_(obs_ids)
                    )
                )
                c.execute(
                    delete(PriceAnomaly).where(PriceAnomaly.price_observation_id.in_(obs_ids))
                )
            c.execute(delete(PriceObservation).where(PriceObservation.retailer_id == rid))
            c.execute(delete(ProductVariant).where(ProductVariant.retailer_id == rid))
            c.execute(delete(ExternalProduct).where(ExternalProduct.retailer_id == rid))
            c.execute(delete(CrawlRun).where(CrawlRun.retailer_id == rid))
            c.execute(delete(Retailer).where(Retailer.id == rid))
            c.commit()
        finally:
            c.close()


def _candidate(rid: int, vid: int, *, amount: str = "1.19", observed_at: datetime = T0):
    return PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", observed_at=observed_at, imported_at=T0,
        valid_from=observed_at, confidence_score=Decimal("1.0"), staging_only=True,
    )


def _counts(rid: int) -> tuple[int, int]:
    c = _session()
    try:
        obs = int(
            c.scalar(
                select(func.count()).select_from(PriceObservation).where(
                    PriceObservation.retailer_id == rid
                )
            )
            or 0
        )
        occ = int(
            c.scalar(
                select(func.count())
                .select_from(PriceObservationOccurrence)
                .join(
                    PriceObservation,
                    PriceObservation.id == PriceObservationOccurrence.price_observation_id,
                )
                .where(PriceObservation.retailer_id == rid)
            )
            or 0
        )
        return obs, occ
    finally:
        c.close()


def _run_concurrently(fns: list) -> None:
    """Run each fn(session) in its own thread; a barrier makes them enter at the same instant."""
    barrier = threading.Barrier(len(fns))
    errors: list[str] = []

    def run(fn):
        s = _session()
        try:
            barrier.wait(timeout=30)
            fn(s)
            s.commit()
        except Exception as exc:
            s.rollback()
            errors.append(repr(exc))
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=(fn,)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


# 1. Same fact + same occurrence simultaneously -> 1 observation, 1 occurrence.
def test_concurrent_same_fact_same_occurrence(seeded) -> None:
    rid, vid, _runs = seeded
    fn = lambda s: record_price_fact(  # noqa: E731
        s, _candidate(rid, vid), OccurrenceProvenance(provider_code="x"), imported_at=T0
    )
    _run_concurrently([fn, fn, fn])
    assert _counts(rid) == (1, 1)


# 2. Same fact + two different CrawlRun -> 1 observation, 2 occurrences.
def test_concurrent_same_fact_two_crawl_runs(seeded) -> None:
    rid, vid, runs = seeded
    r1, r2 = runs

    def make(run_id):
        return lambda s: record_price_fact(
            s, _candidate(rid, vid),
            OccurrenceProvenance(provider_code="x", crawl_run_id=run_id), imported_at=T0,
        )

    _run_concurrently([make(r1), make(r2)])
    assert _counts(rid) == (1, 2)


# 3. Two facts with a different price -> 2 observations.
def test_concurrent_two_prices_two_facts(seeded) -> None:
    rid, vid, _runs = seeded
    _run_concurrently([
        lambda s: record_price_fact(
            s, _candidate(rid, vid, amount="1.19"), OccurrenceProvenance(provider_code="x"),
            imported_at=T0,
        ),
        lambda s: record_price_fact(
            s, _candidate(rid, vid, amount="1.29"), OccurrenceProvenance(provider_code="x"),
            imported_at=T0,
        ),
    ])
    assert _counts(rid)[0] == 2


# 4. Two facts with a different observed_at -> 2 observations.
def test_concurrent_two_observed_at_two_facts(seeded) -> None:
    rid, vid, _runs = seeded
    _run_concurrently([
        lambda s: record_price_fact(
            s, _candidate(rid, vid, observed_at=T0), OccurrenceProvenance(provider_code="x"),
            imported_at=T0,
        ),
        lambda s: record_price_fact(
            s, _candidate(rid, vid, observed_at=T1), OccurrenceProvenance(provider_code="x"),
            imported_at=T0,
        ),
    ])
    assert _counts(rid)[0] == 2


# 5. Rollback of the first transaction releases the lock; the second continues; no partial write.
def test_rollback_releases_lock_and_second_continues(seeded) -> None:
    rid, vid, _runs = seeded
    a = _session()
    try:
        record_price_fact(
            a, _candidate(rid, vid), OccurrenceProvenance(provider_code="x"), imported_at=T0
        )
        a.rollback()  # releases the advisory locks, discards the partial write
    finally:
        a.close()
    assert _counts(rid) == (0, 0)  # nothing left behind

    b = _session()
    try:
        res = record_price_fact(
            b, _candidate(rid, vid), OccurrenceProvenance(provider_code="x"), imported_at=T0
        )
        b.commit()
        assert res.fact_created is True
    finally:
        b.close()
    assert _counts(rid) == (1, 1)


# 6. Exception BETWEEN fact and occurrence -> full rollback, no orphan fact/occurrence.
def test_exception_between_fact_and_occurrence_rolls_back(seeded, monkeypatch) -> None:
    rid, vid, _runs = seeded
    import cestaplan_api.services.observation_persistence as op

    def boom(*_a, **_k):
        raise RuntimeError("injected between fact and occurrence")

    monkeypatch.setattr(op, "_find_existing_occurrence", boom)
    s = _session()
    try:
        with pytest.raises(RuntimeError):
            op.record_price_fact(
                s, _candidate(rid, vid), OccurrenceProvenance(provider_code="x"), imported_at=T0
            )
        s.rollback()
    finally:
        s.close()
    assert _counts(rid) == (0, 0)  # the fact insert was rolled back with the occurrence failure


# 7. Ten concurrent writers of the same fact -> exactly 1 fact; 1 occurrence (same identity).
def test_ten_concurrent_writers_same_fact(seeded) -> None:
    rid, vid, _runs = seeded
    fn = lambda s: record_price_fact(  # noqa: E731
        s, _candidate(rid, vid), OccurrenceProvenance(provider_code="x"), imported_at=T0
    )
    _run_concurrently([fn] * 10)
    assert _counts(rid) == (1, 1)


# 8. Discovery (matching path) still writes NO staging observations — even though _persist_product
# now routes through record_price_fact, matching never ingests. Uses the standard savepoint fixture.
def test_discovery_matching_writes_no_observations(db_session: Session) -> None:
    from cestaplan_api.services.targeted_discovery import ApprovalMode, discover_and_map
    from tests.fixtures.provider_scenarios import (
        ensure_test_ingredient,
        seed_test_catalog_product,
        seed_test_retailer,
    )

    keys = ["leche_entera", "aceite_oliva"]
    for k in keys:
        ensure_test_ingredient(db_session, k)
    retailer = seed_test_retailer(db_session, "carrefour")
    seed_test_catalog_product(db_session, retailer, "CF-CC-1", name="Leche entera 1L", price="1.19")
    before = int(
        db_session.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == retailer.id
            )
        )
        or 0
    )
    discover_and_map(db_session, "parsebot-carrefour", keys, approval_mode=ApprovalMode.REVIEW_ONLY)
    after = int(
        db_session.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == retailer.id
            )
        )
        or 0
    )
    assert after == before  # matching writes candidates only, never observations


# --------------------------------------------------------------------------- #
# Temporal-history concurrency (spec §6): the history-lane lock must keep the interval chain
# coherent (predecessor/successor), never two open rows, under real contention.
# --------------------------------------------------------------------------- #
def _rpf(s: Session, candidate: PriceObservation):
    return record_price_fact(
        s, candidate, OccurrenceProvenance(provider_code="x"), imported_at=T0
    )


def _mk(rid: int, vid: int, *, amount: str, observed_at: datetime):
    """Factory: a thunk that records a fact for (amount, observed_at). Binds its own values."""
    return lambda s: _rpf(s, _candidate(rid, vid, amount=amount, observed_at=observed_at))


def _write(rid: int, vid: int, *, amount: str, observed_at: datetime) -> None:
    s = _session()
    try:
        _rpf(s, _candidate(rid, vid, amount=amount, observed_at=observed_at))
        s.commit()
    finally:
        s.close()


def _active_rows(rid: int) -> list[PriceObservation]:
    c = _session()
    try:
        return list(
            c.execute(
                select(PriceObservation)
                .where(
                    PriceObservation.retailer_id == rid,
                    PriceObservation.verification_status != "disputed",
                    PriceObservation.rolled_back_at.is_(None),
                )
                .order_by(PriceObservation.valid_from)
            ).scalars()
        )
    finally:
        c.close()


def _all_rows(rid: int) -> list[PriceObservation]:
    c = _session()
    try:
        return list(
            c.execute(
                select(PriceObservation).where(
                    PriceObservation.retailer_id == rid,
                    PriceObservation.rolled_back_at.is_(None),
                )
            ).scalars()
        )
    finally:
        c.close()


# T1: two distinct prices, same date/lane, concurrent -> 2 facts, <=1 open (deterministic policy).
def test_temporal_same_date_conflict(seeded) -> None:
    rid, vid, _runs = seeded
    _run_concurrently([
        _mk(rid, vid, amount="1.19", observed_at=T0),
        _mk(rid, vid, amount="1.29", observed_at=T0),
    ])
    rows = _all_rows(rid)
    assert len(rows) == 2  # history keeps both
    open_active = [
        r for r in rows if r.verification_status != "disputed" and r.valid_until is None
    ]
    assert len(open_active) <= 1
    # Documented policy B: both are disputed (no arbitrary current), an anomaly each.
    assert all(r.verification_status == "disputed" for r in rows)
    assert lane_invariants_hold(rows)


# T2: two time points concurrent -> T0.valid_until = T1, T1 open.
def test_temporal_two_points_interval_chain(seeded) -> None:
    rid, vid, _runs = seeded
    _run_concurrently([
        _mk(rid, vid, amount="1.19", observed_at=T0),
        _mk(rid, vid, amount="1.29", observed_at=T1),
    ])
    rows = _active_rows(rid)
    assert len(rows) == 2
    by_from = {r.valid_from: r for r in rows}
    assert by_from[T0].valid_until == T1
    assert by_from[T1].valid_until is None
    assert lane_invariants_hold(_all_rows(rid))


# T3: out-of-order arrival -> insert T1 first, then T0 -> T0.valid_until = T1, T1 stays open.
def test_temporal_out_of_order_slots_between(seeded) -> None:
    rid, vid, _runs = seeded
    _write(rid, vid, amount="1.29", observed_at=T1)  # later point first
    _write(rid, vid, amount="1.19", observed_at=T0)  # earlier point after
    rows = _active_rows(rid)
    by_from = {r.valid_from: r for r in rows}
    assert by_from[T0].valid_until == T1
    assert by_from[T1].valid_until is None  # T1 remains the only open row
    assert lane_invariants_hold(_all_rows(rid))


# T4: three points T0/T1/T2 concurrent (random order) -> intervals T0->T1, T1->T2, T2->null.
def test_temporal_three_points_random_concurrent(seeded) -> None:
    rid, vid, _runs = seeded
    _run_concurrently([
        _mk(rid, vid, amount="1.19", observed_at=T0),
        _mk(rid, vid, amount="1.29", observed_at=T1),
        _mk(rid, vid, amount="1.39", observed_at=T2),
    ])
    rows = _active_rows(rid)
    assert len(rows) == 3
    by_from = {r.valid_from: r for r in rows}
    assert by_from[T0].valid_until == T1
    assert by_from[T1].valid_until == T2
    assert by_from[T2].valid_until is None
    assert lane_invariants_hold(_all_rows(rid))


# T5: ten writers mixing three facts -> exactly 3 facts, <=1 open, occurrences per unique identity.
def test_temporal_ten_writers_three_facts(seeded) -> None:
    rid, vid, _runs = seeded
    specs = [("1.19", T0), ("1.29", T1), ("1.39", T2)]
    fns = [_mk(rid, vid, amount=a, observed_at=t) for (a, t) in (specs[i % 3] for i in range(10))]
    _run_concurrently(fns)
    rows = _active_rows(rid)
    assert len(rows) == 3  # exactly three distinct facts
    assert sum(1 for r in rows if r.valid_until is None) <= 1
    assert _counts(rid) == (3, 3)  # 3 facts, 3 occurrences (same provenance identity)
    assert lane_invariants_hold(_all_rows(rid))


# T7: rollback while holding the lane lock releases it; the next writer reconstructs a valid chain.
def test_temporal_rollback_holding_lane_lock_then_reconstruct(seeded) -> None:
    rid, vid, _runs = seeded
    a = _session()
    try:
        _rpf(a, _candidate(rid, vid, amount="1.19", observed_at=T0))
        a.rollback()
    finally:
        a.close()
    assert _counts(rid) == (0, 0)
    _write(rid, vid, amount="1.19", observed_at=T0)
    _write(rid, vid, amount="1.29", observed_at=T1)
    rows = _active_rows(rid)
    by_from = {r.valid_from: r for r in rows}
    assert by_from[T0].valid_until == T1 and by_from[T1].valid_until is None
    assert lane_invariants_hold(_all_rows(rid))


# T8: lane-lock timeout -> explicit error, zero partial writes.
def test_temporal_lane_lock_timeout_no_partial_write(seeded) -> None:
    rid, vid, _runs = seeded
    key = ident.price_history_lane_lock_key(
        _candidate(rid, vid, amount="1.19", observed_at=T0)
    )
    holder = _session()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})  # hold the lane lock
        b = _session()
        try:
            with pytest.raises(Exception):  # noqa: B017 - lock_not_available surfaces
                record_price_fact(
                    b, _candidate(rid, vid, amount="1.19", observed_at=T0),
                    OccurrenceProvenance(provider_code="x"), imported_at=T0, lock_timeout_ms=200,
                )
            b.rollback()
        finally:
            b.close()
    finally:
        holder.rollback()
        holder.close()
    assert _counts(rid) == (0, 0)  # nothing written
