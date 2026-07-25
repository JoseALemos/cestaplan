"""Exact-duplicate staging-observation cleanup: only technically-identical facts are removed, a
reversible manifest is written, --apply is gated on the expected count, and a real price /
store difference is never treated as a duplicate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import PriceObservation, ProductPrice
from cestaplan_api.tools import dedup_staging_observations as dedup
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
    seed_test_store,
)

PROVIDER = "test_dedup_provider"  # not in the matrix -> slug == provider_code (isolated retailer)
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)


def _obs(db, retailer_id, variant_id, *, amount, observed_at, scope="national", store=None, imp=T0):
    o = PriceObservation(
        retailer_id=retailer_id,
        product_variant_id=variant_id,
        store_id=store,
        price_scope=scope,
        price_type="regular",
        amount=Decimal(amount),
        currency="EUR",
        observed_at=observed_at,
        imported_at=imp,
        valid_from=observed_at,
        confidence_score=Decimal("1.0"),
        staging_only=True,
    )
    db.add(o)
    db.flush()
    return o


def _scenario(db: Session):
    retailer = seed_test_retailer(db, PROVIDER)
    store = seed_test_store(db, retailer)
    _p, variant = seed_test_catalog_product(db, retailer, "TD-1", name="Producto test", price=None)
    rid, vid = retailer.id, variant.id
    # 3 EXACT duplicates (same fact; only imported_at/id differ) -> 2 removable.
    _obs(db, rid, vid, amount="1.19", observed_at=T0, imp=T0)
    _obs(db, rid, vid, amount="1.19", observed_at=T0, imp=T0 + timedelta(hours=1))
    _obs(db, rid, vid, amount="1.19", observed_at=T0, imp=T0 + timedelta(hours=2))
    # Real differences — never duplicates:
    _obs(db, rid, vid, amount="1.29", observed_at=T0)  # price change
    _obs(db, rid, vid, amount="1.19", observed_at=T1)  # observed_at change
    _obs(db, rid, vid, amount="1.19", observed_at=T0, store=store.id)  # store change
    return retailer, variant


def _staging_count(db, rid) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(PriceObservation).where(
                PriceObservation.retailer_id == rid, PriceObservation.staging_only.is_(True)
            )
        )
        or 0
    )


def test_dry_run_finds_only_exact_duplicates_and_writes_nothing(db_session: Session) -> None:
    retailer, _v = _scenario(db_session)
    before = _staging_count(db_session, retailer.id)
    report = dedup.dry_run(db_session, PROVIDER)
    assert report["duplicate_groups"] == 1
    assert report["removable_exact_duplicates"] == 2  # the 4 non-dup rows are untouched
    assert _staging_count(db_session, retailer.id) == before  # dry-run wrote nothing


def test_apply_requires_matching_expected_count(db_session: Session) -> None:
    _scenario(db_session)
    with pytest.raises(SystemExit):
        dedup.apply(db_session, PROVIDER, expected_delete_count=145, manifest_path=None)
    # nothing deleted on abort
    assert dedup.dry_run(db_session, PROVIDER)["removable_exact_duplicates"] == 2


def test_apply_deletes_exactly_and_keeps_real_facts(db_session: Session) -> None:
    retailer, _v = _scenario(db_session)
    price_before = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    before = _staging_count(db_session, retailer.id)

    result = dedup.apply(db_session, PROVIDER, expected_delete_count=2, manifest_path=None)
    assert result["deleted_count"] == 2
    assert result["remaining_exact_duplicates"] == 0

    # 2 removed, the 4 distinct facts (canonical + price/observed_at/store variants) remain.
    assert _staging_count(db_session, retailer.id) == before - 2
    assert dedup.dry_run(db_session, PROVIDER)["removable_exact_duplicates"] == 0
    # never touched productive prices
    price_after = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    assert price_after == price_before

    # The manifest is reconstructable (proof-only: rolled back, not re-inserted permanently).
    restored = dedup.restore_manifest(db_session, result["manifest_id"], apply_restore=False)
    assert restored["restorable_rows"] == 2
    assert restored["reconstructed"] == 2
    # rollback means the count is unchanged after the proof restore.
    assert _staging_count(db_session, retailer.id) == before - 2
