"""§3: a staging sync into a history lane that is ALREADY corrupt refuses the write and surfaces it
as a QUALITY rejection (blocked_lane_anomalies + a reason) — never an empty catalogue, never an
auto-repair. Reuses the acceptance-test fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.ingestion.providers.test_acceptance_e2e import (
    _FakeProvider,
    _prod,
    _retailer,
    _settings,
)

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.models import PriceObservation
from cestaplan_api.services.provider_sync import SyncMode, run_provider_sync

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


def test_staging_sync_surfaces_preexisting_lane_block(db_session: Session) -> None:
    retailer = _retailer(db_session)
    settings = _settings()
    products = [
        _prod("800001", "1.00", NOW, scope=PriceScope.NATIONAL),
        _prod("800002", "2.00", NOW, scope=PriceScope.NATIONAL),
    ]

    r1 = run_provider_sync(
        db_session, _FakeProvider(products), retailer, settings, mode=SyncMode.STAGING, as_of=NOW
    )
    assert r1.blocked_lane_anomalies == 0
    assert r1.observations_created == 2

    # Corrupt ONE product's lane: clone its open staging row into a SECOND open row (two open rows).
    row = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.retailer_id == retailer.id, PriceObservation.staging_only.is_(True))
        .order_by(PriceObservation.id)
        .limit(1)
    ).scalars().first()
    assert row is not None
    db_session.add(
        PriceObservation(
            retailer_id=row.retailer_id, product_variant_id=row.product_variant_id,
            store_id=row.store_id, delivery_zone_id=row.delivery_zone_id,
            price_scope=row.price_scope, price_type=row.price_type, currency=row.currency,
            staging_only=True, amount=row.amount + Decimal("5"),
            observed_at=NOW + timedelta(days=1), imported_at=NOW,
            valid_from=NOW + timedelta(days=1), valid_until=None, confidence_score=Decimal("1.0"),
        )
    )
    db_session.flush()

    # A second staging sync: the corrupt lane is refused and reported; the other product is fine.
    r2 = run_provider_sync(
        db_session, _FakeProvider(products), retailer, settings,
        mode=SyncMode.STAGING, as_of=NOW + timedelta(days=2),
    )
    assert r2.blocked_lane_anomalies == 1
    assert any("lane_anomaly_blocked" in reason for reason in r2.reasons)
