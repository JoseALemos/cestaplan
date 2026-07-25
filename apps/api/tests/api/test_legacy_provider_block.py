"""Staging-first migration: the legacy direct-productive-write paths are BLOCKED by default.

Open Prices sync (Product/ProductBarcode/ProductPrice) and active ingredient mapping must not run
without an explicit, audited promotion. A blocked call raises and changes NO productive rows.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice, Store
from cestaplan_api.routers.admin import OpenPricesSyncIn, sync_all_sources, sync_open_prices
from cestaplan_api.services import ingredient_matching, open_prices_sync
from cestaplan_api.services.open_prices_sync import (
    LegacyProviderWriteBlocked,
    guard_legacy_provider_writes,
)


def _counts(db: Session) -> tuple[int, int]:
    return (
        int(db.scalar(select(func.count()).select_from(Product)) or 0),
        int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0),
    )


def test_guard_blocks_by_default() -> None:
    # No env enables it -> legacy_direct_provider_writes_enabled is False by default.
    assert open_prices_sync.get_settings().legacy_direct_provider_writes_enabled is False
    with pytest.raises(LegacyProviderWriteBlocked):
        guard_legacy_provider_writes()


def test_sync_store_blocked_changes_no_productive_rows(db_session: Session) -> None:
    store = db_session.execute(
        select(Store).where(Store.is_synthetic.is_(True)).order_by(Store.id)
    ).scalars().first()
    assert store is not None
    before = _counts(db_session)
    with pytest.raises(LegacyProviderWriteBlocked):
        open_prices_sync.sync_store(db_session, store)
    with pytest.raises(LegacyProviderWriteBlocked):
        open_prices_sync.sync_all(db_session)
    assert _counts(db_session) == before  # no Product / ProductPrice written


def test_map_real_products_blocked(db_session: Session) -> None:
    with pytest.raises(LegacyProviderWriteBlocked):
        ingredient_matching.map_real_products(db_session)


def test_open_prices_sync_endpoint_is_409_by_default(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc:
        sync_open_prices(OpenPricesSyncIn(), None, db_session)  # type: ignore[arg-type]
    assert exc.value.status_code == 409


def test_sync_all_endpoint_is_409_by_default(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc:
        sync_all_sources(None, db_session)  # type: ignore[arg-type]
    assert exc.value.status_code == 409
