"""Dedup with the two-layer model (spec §6/§7): duplicate PriceObservations are grouped by fact
identity; every occurrence is relinked to the canonical fact (or dropped when the canonical already
has that provenance) BEFORE the duplicate row is deleted, so no crawl/capture evidence is ever lost.
A group with an unresolvable incoming FK is excluded. Deletion stays reversible (exact_restore)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    PriceObservation,
    PriceObservationOccurrence,
    PromotionRule,
)
from cestaplan_api.tools import dedup_staging_observations as dedup
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
)

PROVIDER = "test_relink_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


def _run(db: Session, retailer_id: int) -> int:
    run = CrawlRun(retailer_id=retailer_id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _obs(db, rid, vid, run_id, *, imp):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal("1.19"), currency="EUR", observed_at=T0, imported_at=imp, valid_from=T0,
        confidence_score=Decimal("1.0"), staging_only=True, crawl_run_id=run_id,
    )
    db.add(o)
    db.flush()
    return o


def _occ(db, obs_id, run_id):
    occ = PriceObservationOccurrence(
        price_observation_id=obs_id, provider_code=PROVIDER, crawl_run_id=run_id, imported_at=T0,
    )
    db.add(occ)
    db.flush()
    return occ


def _occ_count(db, obs_id) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservationOccurrence).where(
                PriceObservationOccurrence.price_observation_id == obs_id
            )
        )
        or 0
    )


def _two_dups_distinct_provenance(db: Session):
    """Two exact-duplicate facts, each confirmed by a DIFFERENT crawl (distinct provenance)."""
    retailer = seed_test_retailer(db, PROVIDER)
    _p, variant = seed_test_catalog_product(db, retailer, "RL-1", name="Relink", price=None)
    run_a, run_b = _run(db, retailer.id), _run(db, retailer.id)
    canonical = _obs(db, retailer.id, variant.id, run_a, imp=T0)
    removed = _obs(db, retailer.id, variant.id, run_b, imp=T0 + timedelta(hours=1))
    _occ(db, canonical.id, run_a)
    _occ(db, removed.id, run_b)
    return retailer, canonical, removed, run_a, run_b


def test_dry_run_counts_occurrences_to_relink(db_session: Session) -> None:
    _retailer, _c, _r, _ra, _rb = _two_dups_distinct_provenance(db_session)
    d = dedup.dry_run(db_session, PROVIDER)
    assert d["duplicate_fact_groups"] == 1
    assert d["removable_price_observations"] == 1
    assert d["occurrences_to_relink"] == 1  # removed's occurrence must move to canonical
    assert d["occurrences_already_present"] == 0
    assert d["new_real_count"] == d["staging_observations"] - 1


def test_apply_relinks_occurrence_before_delete(db_session: Session) -> None:
    _retailer, canonical, removed, run_a, run_b = _two_dups_distinct_provenance(db_session)
    removed_id = removed.id
    res = dedup.apply(db_session, PROVIDER, expected_delete_count=1, manifest_path=None)
    assert res["deleted_count"] == 1
    assert res["occurrences_relinked"] == 1 and res["occurrences_dropped"] == 0
    # The removed row is gone; its occurrence now lives on the canonical (both provenances kept).
    assert db_session.get(PriceObservation, removed_id) is None
    assert _occ_count(db_session, canonical.id) == 2
    runs = set(
        db_session.execute(
            select(PriceObservationOccurrence.crawl_run_id).where(
                PriceObservationOccurrence.price_observation_id == canonical.id
            )
        ).scalars()
    )
    assert runs == {run_a, run_b}  # no provenance lost


def test_duplicate_provenance_is_dropped_not_relinked(db_session: Session) -> None:
    retailer = seed_test_retailer(db_session, PROVIDER)
    _p, variant = seed_test_catalog_product(db_session, retailer, "RL-2", name="R2", price=None)
    run = _run(db_session, retailer.id)
    canonical = _obs(db_session, retailer.id, variant.id, run, imp=T0)
    removed = _obs(db_session, retailer.id, variant.id, run, imp=T0 + timedelta(hours=1))
    # BOTH occurrences share the same provenance tuple (same crawl run) -> the removed one is a dup.
    _occ(db_session, canonical.id, run)
    _occ(db_session, removed.id, run)

    d = dedup.dry_run(db_session, PROVIDER)
    assert d["occurrences_to_relink"] == 0 and d["occurrences_already_present"] == 1

    res = dedup.apply(db_session, PROVIDER, expected_delete_count=1, manifest_path=None)
    assert res["occurrences_dropped"] == 1 and res["occurrences_relinked"] == 0
    assert _occ_count(db_session, canonical.id) == 1  # canonical keeps its single evidence


def test_exact_restore_reverses_relink_and_drop(db_session: Session) -> None:
    _retailer, canonical, removed, _run_a, _run_b = _two_dups_distinct_provenance(db_session)
    removed_id = removed.id
    res = dedup.apply(db_session, PROVIDER, expected_delete_count=1, manifest_path=None)

    restored = dedup.restore_manifest(db_session, res["manifest_id"], commit=False)
    # The proof lives in the returned counts; commit=False rolls the reconstruction back.
    assert restored["restore_type"] == "exact_restore"
    assert restored["reconstructed"] == 1 and restored["hash_matches"] == 1
    assert restored["occurrences_relinked_back"] == 1
    # Rolled back -> DB is back to the post-apply state (removed gone, both occ on canonical).
    assert db_session.get(PriceObservation, removed_id) is None
    assert _occ_count(db_session, canonical.id) == 2


def test_unresolvable_fk_excludes_group(db_session: Session) -> None:
    _retailer, _canonical, removed, _ra, _rb = _two_dups_distinct_provenance(db_session)
    # The ONLY removable row gains a blocking incoming FK -> the whole group is excluded, nothing
    # is removable, and its occurrences are never touched.
    db_session.add(PromotionRule(price_observation_id=removed.id, type="percentage"))
    db_session.flush()
    d = dedup.dry_run(db_session, PROVIDER)
    assert d["rows_with_fk_dependencies"] == 1
    assert d["excluded_groups"] == 1
    assert d["removable_price_observations"] == 0
    assert _occ_count(db_session, removed.id) == 1  # untouched
