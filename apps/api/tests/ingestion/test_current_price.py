"""Current-price read + freshness + ProductPrice projection (live Postgres, no network)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import NormalizedObservation, PriceScope, PriceType
from cestaplan_api.ingestion.current_price import (
    CurrentPriceService,
    FreshnessStatus,
)
from cestaplan_api.ingestion.price_history import record_observation
from cestaplan_api.models import (
    ExternalProduct,
    Product,
    ProductPrice,
    ProductVariant,
    Retailer,
    Store,
)


@dataclass(slots=True)
class Fixture:
    retailer_id: int
    store_id: int
    product_id: int
    variant: ProductVariant


@pytest.fixture()
def priced_variant(db_session: Session) -> Fixture:
    retailer = Retailer(
        slug="cp-retailer", name="CP Retailer", adapter_key="test", is_synthetic=True
    )
    db_session.add(retailer)
    db_session.flush()

    store = Store(retailer_id=retailer.id, name="CP Store", is_synthetic=True)
    product = Product(name="CP Product", is_synthetic=True)
    db_session.add_all([store, product])
    db_session.flush()

    external = ExternalProduct(retailer_id=retailer.id, external_id="CP-1")
    db_session.add(external)
    db_session.flush()

    pv = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name="CP Variant 1kg",
        package_quantity=Decimal("1"),
        package_unit="kg",
    )
    db_session.add(pv)
    db_session.flush()
    return Fixture(
        retailer_id=retailer.id,
        store_id=store.id,
        product_id=product.id,
        variant=pv,
    )


def _obs(amount: str, *, observed_at: datetime) -> NormalizedObservation:
    return NormalizedObservation(
        variant_ref="CP-1",
        amount=Decimal(amount),
        currency="EUR",
        price_scope=PriceScope.EXACT_STORE,
        price_type=PriceType.REGULAR,
        observed_at=observed_at,
    )


def test_current_returns_latest_valid_with_fresh_status(
    db_session: Session, priced_variant: Fixture
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=5)
    t1 = datetime.now(UTC) - timedelta(hours=1)
    as_of = datetime.now(UTC)

    record_observation(
        db_session,
        _obs("1.50", observed_at=t0),
        product_variant_id=priced_variant.variant.id,
        retailer_id=priced_variant.retailer_id,
        store_id=priced_variant.store_id,
        as_of=t0,
    )
    record_observation(
        db_session,
        _obs("1.80", observed_at=t1),
        product_variant_id=priced_variant.variant.id,
        retailer_id=priced_variant.retailer_id,
        store_id=priced_variant.store_id,
        as_of=t1,
    )

    svc = CurrentPriceService()
    current = svc.current(
        db_session,
        priced_variant.variant.id,
        store_id=priced_variant.store_id,
        as_of=as_of,
    )
    assert current is not None
    assert current.amount == Decimal("1.80")  # latest wins
    assert current.status is FreshnessStatus.FRESH
    assert current.age == as_of - t1


def test_current_status_stale_then_expired(
    db_session: Session, priced_variant: Fixture
) -> None:
    observed = datetime.now(UTC) - timedelta(hours=30)
    as_of = datetime.now(UTC)
    record_observation(
        db_session,
        _obs("2.00", observed_at=observed),
        product_variant_id=priced_variant.variant.id,
        retailer_id=priced_variant.retailer_id,
        store_id=priced_variant.store_id,
        as_of=observed,
    )
    svc = CurrentPriceService()

    stale = svc.current(
        db_session, priced_variant.variant.id, store_id=priced_variant.store_id, as_of=as_of
    )
    assert stale is not None
    assert stale.status is FreshnessStatus.STALE  # 30h: past 24h stale threshold

    later = as_of + timedelta(hours=30)  # total age 60h > 48h expired threshold
    expired = svc.current(
        db_session,
        priced_variant.variant.id,
        store_id=priced_variant.store_id,
        as_of=later,
    )
    assert expired is not None
    assert expired.status is FreshnessStatus.EXPIRED


def test_project_current_prices_upserts_product_price(
    db_session: Session, priced_variant: Fixture
) -> None:
    observed = datetime.now(UTC) - timedelta(hours=1)
    record_observation(
        db_session,
        _obs("3.25", observed_at=observed),
        product_variant_id=priced_variant.variant.id,
        retailer_id=priced_variant.retailer_id,
        store_id=priced_variant.store_id,
        as_of=observed,
    )

    svc = CurrentPriceService()
    written = svc.project_current_prices(db_session, priced_variant.retailer_id)
    assert written == 1

    price = db_session.execute(
        select(ProductPrice).where(
            ProductPrice.product_id == priced_variant.product_id,
            ProductPrice.store_id == priced_variant.store_id,
        )
    ).scalar_one()
    assert price.amount == Decimal("3.25")
    assert price.is_synthetic is False
    assert price.package_unit == "kg"

    # Idempotent: same observed_at is not duplicated.
    again = svc.project_current_prices(db_session, priced_variant.retailer_id)
    assert again == 0


def test_current_none_when_no_observation(
    db_session: Session, priced_variant: Fixture
) -> None:
    svc = CurrentPriceService()
    assert (
        svc.current(
            db_session,
            priced_variant.variant.id,
            store_id=priced_variant.store_id,
            as_of=datetime.now(UTC),
        )
        is None
    )
