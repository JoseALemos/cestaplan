"""Rolled-back observations are never a current price (spec §1). By model contract a row with
``rolled_back_at`` is ignored by current/latest/selectable/costable/shadow/projection — the filter
is on ``rolled_back_at``, not on ``valid_until``/``verification_status`` alone."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.models import (
    ExternalProduct,
    PriceObservation,
    Product,
    ProductPrice,
    ProductVariant,
    Retailer,
    Store,
)

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


@pytest.fixture()
def variant(db_session: Session):
    retailer = Retailer(slug="rb-ret", name="RB", adapter_key="test", is_synthetic=True)
    db_session.add(retailer)
    db_session.flush()
    store = Store(retailer_id=retailer.id, name="RB Store", is_synthetic=True)
    product = Product(name="RB Product", is_synthetic=True)
    db_session.add_all([store, product])
    db_session.flush()
    external = ExternalProduct(retailer_id=retailer.id, external_id="RB-1")
    db_session.add(external)
    db_session.flush()
    pv = ProductVariant(
        product_id=product.id, retailer_id=retailer.id, external_product_id=external.id,
        display_name="RB Variant", package_quantity=Decimal("1"), package_unit="kg",
    )
    db_session.add(pv)
    db_session.flush()
    return retailer, store, product, pv


def _obs(db, retailer, pv, *, amount, valid_from, valid_until, staging=True, store=None,
         rolled_back=False, disputed=False):
    o = PriceObservation(
        retailer_id=retailer.id, product_variant_id=pv.id, store_id=store,
        price_scope="national", price_type="regular", amount=Decimal(amount), currency="EUR",
        observed_at=valid_from, imported_at=valid_from, valid_from=valid_from,
        valid_until=valid_until, confidence_score=Decimal("1.0"), staging_only=staging,
        rolled_back_at=(valid_from if rolled_back else None),
        verification_status=("disputed" if disputed else "unverified"),
    )
    db.add(o)
    db.flush()
    return o


def test_rolled_back_open_is_skipped_for_a_valid_row(variant, db_session: Session) -> None:
    retailer, _store, _product, pv = variant
    _obs(db_session, retailer, pv, amount="1.00", valid_from=T0, valid_until=None)  # valid, open
    _obs(db_session, retailer, pv, amount="9.99", valid_from=T1, valid_until=None, rolled_back=True)
    cur = CurrentPriceService().current(db_session, pv.id, as_of=NOW, staging=True)
    assert cur is not None and cur.amount == Decimal("1.00")  # never the rolled-back row


def test_only_rolled_back_returns_none(variant, db_session: Session) -> None:
    retailer, _store, _product, pv = variant
    _obs(db_session, retailer, pv, amount="1.00", valid_from=T0, valid_until=None, rolled_back=True)
    assert CurrentPriceService().current(db_session, pv.id, as_of=NOW, staging=True) is None


def test_rolled_back_inside_staging_interval_not_selected(variant, db_session: Session) -> None:
    retailer, _store, _product, pv = variant
    # A rolled-back row whose interval CONTAINS as_of must still not be selected.
    _obs(db_session, retailer, pv, amount="1.00", valid_from=T0, valid_until=T2, rolled_back=True)
    assert CurrentPriceService().current(db_session, pv.id, as_of=T1, staging=True) is None


def test_disputed_and_rolled_back_never_selected(variant, db_session: Session) -> None:
    retailer, _store, _product, pv = variant
    _obs(db_session, retailer, pv, amount="1.00", valid_from=T0, valid_until=T0,
         rolled_back=True, disputed=True)
    assert CurrentPriceService().current(db_session, pv.id, as_of=NOW, staging=True) is None


def test_project_current_prices_skips_rolled_back(variant, db_session: Session) -> None:
    retailer, store, _product, pv = variant
    before = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    # A production (non-staging) rolled-back open observation must never project to ProductPrice.
    _obs(db_session, retailer, pv, amount="1.00", valid_from=T0, valid_until=None,
         staging=False, store=store.id, rolled_back=True)
    written = CurrentPriceService().project_current_prices(db_session, retailer.id)
    after = int(db_session.scalar(select(func.count()).select_from(ProductPrice)) or 0)
    assert written == 0 and after == before  # nothing projected from a rolled-back row
