"""Hardened exact-duplicate cleanup: full-column fact identity, provider provenance, incoming-FK
exclusion, expected-count gate, rich reversible manifest and EXACT restore (original id + hash)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    PriceObservation,
    ProductPrice,
    PromotionRule,
)
from cestaplan_api.services import observation_identity as ident
from cestaplan_api.tools import dedup_staging_observations as dedup
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
    seed_test_store,
)

PROVIDER = "test_dedup_provider"  # not in the matrix -> slug == provider_code (isolated retailer)
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)

# The classification of PriceObservation columns is deliberate; a NEW column must fail this until it
# is consciously placed in TECHNICAL_FIELDS or accepted as part of the fact.
_EXPECTED_COLUMNS = {
    "amount", "available", "closed_by_run_id", "confidence_score", "connector_version",
    "crawl_run_id", "created_at", "currency", "delivery_zone_id", "expires_at", "id", "imported_at",
    "observed_at", "parser_version", "price_scope", "price_type", "product_variant_id",
    "promotion_text", "promotion_valid_from", "promotion_valid_until", "public_id",
    "raw_capture_id",
    "requires_loyalty", "retailer_id", "rolled_back_at", "rolled_back_by", "source_id", "source_url",
    "staging_only", "store_id", "unit_amount", "unit_code", "updated_at", "valid_from", "valid_until",
    "verification_status",
}


def test_column_classification_is_complete_and_conscious() -> None:
    assert set(ident.all_columns()) == _EXPECTED_COLUMNS  # fails when a column is added/removed
    assert set(ident.all_columns()) >= ident.TECHNICAL_FIELDS
    # No technical field leaks into the fact identity.
    assert not (set(ident.semantic_columns()) & ident.TECHNICAL_FIELDS)


def _obs(db, rid, vid, run_id, *, amount, observed_at, store=None, imp=T0):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, store_id=store, crawl_run_id=run_id,
        price_scope="national", price_type="regular", amount=Decimal(amount), currency="EUR",
        observed_at=observed_at, imported_at=imp, valid_from=observed_at,
        confidence_score=Decimal("1.0"), staging_only=True,
    )
    db.add(o)
    db.flush()
    return o


def _scenario(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    store = seed_test_store(db, retailer)
    run = CrawlRun(retailer_id=retailer.id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    _p, variant = seed_test_catalog_product(db, retailer, "TD-1", name="Producto test", price=None)
    rid, vid = retailer.id, variant.id
    # 3 EXACT duplicates (verified provenance via crawl_run) -> 2 removable.
    d1 = _obs(db, rid, vid, run.id, amount="1.19", observed_at=T0, imp=T0)
    _obs(db, rid, vid, run.id, amount="1.19", observed_at=T0, imp=T0 + timedelta(hours=1))
    _obs(db, rid, vid, run.id, amount="1.19", observed_at=T0, imp=T0 + timedelta(hours=2))
    # Real differences — never duplicates:
    _obs(db, rid, vid, run.id, amount="1.29", observed_at=T0)
    _obs(db, rid, vid, run.id, amount="1.19", observed_at=T1)
    _obs(db, rid, vid, run.id, amount="1.19", observed_at=T0, store=store.id)
    return retailer, variant, run, d1


def _staging_count(db, rid) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == rid, PriceObservation.staging_only.is_(True)
            )
        ) or 0
    )


def test_dry_run_verified_provenance_and_exact_only(db_session: Session) -> None:
    retailer, _v, _run, _d = _scenario(db_session)
    before = _staging_count(db_session, retailer.id)
    r = dedup.dry_run(db_session, PROVIDER)
    assert r["duplicate_groups"] == 1
    assert r["removable_exact_duplicates"] == 2
    assert r["rows_with_verified_provider"] == 2
    assert r["rows_with_ambiguous_provider"] == 0
    assert _staging_count(db_session, retailer.id) == before  # dry-run wrote nothing


def test_ambiguous_provenance_is_excluded(db_session: Session) -> None:
    retailer, variant, _run, _d = _scenario(db_session)
    # A distinct fact (amount 2.99) duplicated WITHOUT any crawl_run -> both rows have ambiguous
    # provenance, so the removed one is excluded (kept), never auto-deleted.
    _obs(db_session, retailer.id, variant.id, None, amount="2.99", observed_at=T0, imp=T0)
    _obs(db_session, retailer.id, variant.id, None, amount="2.99", observed_at=T0, imp=T1)
    r = dedup.dry_run(db_session, PROVIDER)
    assert r["rows_with_ambiguous_provider"] == 1
    assert r["excluded_ambiguous_rows"] == 1
    # still only the 2 verified 1.19 duplicates are removable.
    assert r["removable_exact_duplicates"] == 2


def test_referenced_row_is_excluded(db_session: Session) -> None:
    retailer, _v, _run, d1 = _scenario(db_session)
    # d1 is the canonical (earliest). Add a promotion rule to a REMOVED duplicate instead.
    removed = dedup.analyze(db_session, PROVIDER)["groups"][0]["removed_observation_ids"]
    db_session.add(PromotionRule(price_observation_id=removed[0], type="percentage"))
    db_session.flush()
    r = dedup.dry_run(db_session, PROVIDER)
    assert r["referenced_rows"] == 1
    assert r["excluded_due_to_references"] == 1
    assert "promotion_rule" in r["reference_tables"]
    assert r["removable_exact_duplicates"] == 1  # the referenced duplicate is kept


def test_apply_requires_and_matches_expected_count(db_session: Session) -> None:
    _scenario(db_session)
    with pytest.raises(SystemExit):
        dedup.apply(db_session, PROVIDER, expected_delete_count=145, manifest_path=None)
    assert dedup.dry_run(db_session, PROVIDER)["removable_exact_duplicates"] == 2


def test_apply_deletes_and_exact_restore_matches_hash(db_session: Session) -> None:
    retailer, _v, _run, _d = _scenario(db_session)
    price_before = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    before = _staging_count(db_session, retailer.id)

    res = dedup.apply(db_session, PROVIDER, expected_delete_count=2, manifest_path=None)
    assert res["deleted_count"] == 2 and res["remaining_exact_duplicates"] == 0
    assert _staging_count(db_session, retailer.id) == before - 2
    assert dedup.dry_run(db_session, PROVIDER)["removable_exact_duplicates"] == 0
    price_after = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    assert price_after == price_before

    # EXACT restore inside a rolled-back txn: reconstructs with original id, hashes match.
    restored = dedup.restore_manifest(db_session, res["manifest_id"], commit=False)
    assert restored["restore_type"] == "exact_restore"
    assert restored["restorable_rows"] == 2
    assert restored["reconstructed"] == 2
    assert restored["hash_matches"] == 2
    assert _staging_count(db_session, retailer.id) == before - 2  # proof-only, rolled back
