"""Coverage snapshot computation + persistence (live Postgres, no network)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import CoverageStatus, NormalizedObservation, PriceScope, PriceType
from cestaplan_api.ingestion.coverage import PriceCoverageService
from cestaplan_api.ingestion.price_history import record_observation
from cestaplan_api.models import (
    CoverageSnapshot,
    ExternalProduct,
    ProductVariant,
    Retailer,
    Store,
)


@dataclass(slots=True)
class Catalog:
    retailer_id: int
    store_id: int
    variant_ids: list[int]


@pytest.fixture()
def catalog(db_session: Session) -> Catalog:
    retailer = Retailer(
        slug="cov-retailer", name="Cov Retailer", adapter_key="test", is_synthetic=True
    )
    db_session.add(retailer)
    db_session.flush()
    store = Store(retailer_id=retailer.id, name="Cov Store", is_synthetic=True)
    db_session.add(store)
    db_session.flush()

    variant_ids: list[int] = []
    for i in range(4):
        external = ExternalProduct(retailer_id=retailer.id, external_id=f"COV-{i}")
        db_session.add(external)
        db_session.flush()
        pv = ProductVariant(
            retailer_id=retailer.id,
            external_product_id=external.id,
            display_name=f"Cov Variant {i}",
        )
        db_session.add(pv)
        db_session.flush()
        variant_ids.append(pv.id)
    return Catalog(
        retailer_id=retailer.id, store_id=store.id, variant_ids=variant_ids
    )


def _record(db, catalog: Catalog, variant_id: int, amount: str, observed_at: datetime) -> None:
    record_observation(
        db,
        NormalizedObservation(
            variant_ref="x",
            amount=Decimal(amount),
            currency="EUR",
            price_scope=PriceScope.EXACT_STORE,
            price_type=PriceType.REGULAR,
            observed_at=observed_at,
        ),
        product_variant_id=variant_id,
        retailer_id=catalog.retailer_id,
        store_id=catalog.store_id,
        as_of=observed_at,
    )


def test_partial_coverage_reports_partial(
    db_session: Session, catalog: Catalog
) -> None:
    as_of = datetime.now(UTC)
    fresh = as_of - timedelta(hours=1)
    # Price 2 of 4 variants -> ratio 0.5 -> PARTIAL.
    _record(db_session, catalog, catalog.variant_ids[0], "1.00", fresh)
    _record(db_session, catalog, catalog.variant_ids[1], "2.00", fresh)

    svc = PriceCoverageService()
    snap = svc.snapshot(
        db_session, catalog.retailer_id, store_id=catalog.store_id, as_of=as_of
    )
    assert snap.discovered_products == 4
    assert snap.priced_products == 2
    assert snap.fresh_prices == 2
    assert snap.coverage_ratio == Decimal("0.5000")
    assert snap.status == CoverageStatus.PARTIAL.value

    # Persisted and readable.
    persisted = db_session.execute(
        select(CoverageSnapshot).where(CoverageSnapshot.id == snap.id)
    ).scalar_one()
    assert persisted.priced_products == 2
    latest = svc.latest_coverage(
        db_session, catalog.retailer_id, store_id=catalog.store_id
    )
    assert latest is not None
    assert latest.id == snap.id


def test_empty_coverage_is_none(db_session: Session, catalog: Catalog) -> None:
    as_of = datetime.now(UTC)
    svc = PriceCoverageService()
    snap = svc.snapshot(
        db_session, catalog.retailer_id, store_id=catalog.store_id, as_of=as_of
    )
    assert snap.priced_products == 0
    assert snap.coverage_ratio == Decimal("0.0000")
    assert snap.status == CoverageStatus.NONE.value


def test_all_stale_coverage_is_stale(db_session: Session, catalog: Catalog) -> None:
    as_of = datetime.now(UTC)
    old = as_of - timedelta(hours=30)  # past 24h stale threshold
    for variant_id in catalog.variant_ids:
        _record(db_session, catalog, variant_id, "1.00", old)

    svc = PriceCoverageService()
    snap = svc.snapshot(
        db_session, catalog.retailer_id, store_id=catalog.store_id, as_of=as_of
    )
    assert snap.priced_products == 4
    assert snap.fresh_prices == 0
    assert snap.status == CoverageStatus.STALE.value
