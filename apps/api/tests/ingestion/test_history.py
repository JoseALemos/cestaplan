"""Append-only price-history behaviour (live Postgres, no network).

Covers record_observation: a price change closes the prior interval and appends a new open
row (both retained); an unchanged re-observation revalidates without duplicating; a
quarantined observation never replaces the last-good open row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import (
    NormalizedObservation,
    PriceScope,
    PriceType,
)
from cestaplan_api.ingestion.price_history import record_observation
from cestaplan_api.models import PriceAnomaly, PriceObservation, ProductVariant


def _obs(amount: str, *, observed_at: datetime, promotion=None) -> NormalizedObservation:
    return NormalizedObservation(
        variant_ref="EXT-1",
        amount=Decimal(amount),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        price_type=PriceType.REGULAR,
        observed_at=observed_at,
        promotion=promotion,
    )


def test_price_change_closes_old_and_appends_new(
    db_session: Session, variant: ProductVariant
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = datetime.now(UTC)

    first = record_observation(
        db_session,
        _obs("1.50", observed_at=t0),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t0,
    )
    second = record_observation(
        db_session,
        _obs("1.75", observed_at=t1),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t1,
    )

    rows = (
        db_session.execute(
            select(PriceObservation)
            .where(PriceObservation.product_variant_id == variant.id)
            .order_by(PriceObservation.valid_from)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2  # history preserved, not overwritten
    assert rows[0].id == first.id
    assert rows[0].valid_until == t1  # prior interval closed at as_of
    assert rows[1].id == second.id
    assert rows[1].valid_until is None  # new open interval
    assert rows[1].amount == Decimal("1.75")


def test_unchanged_reobservation_does_not_duplicate(
    db_session: Session, variant: ProductVariant
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    t1 = datetime.now(UTC)

    first = record_observation(
        db_session,
        _obs("2.00", observed_at=t0),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t0,
    )
    again = record_observation(
        db_session,
        _obs("2.00", observed_at=t1),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t1,
    )

    count = db_session.execute(
        select(func.count())
        .select_from(PriceObservation)
        .where(PriceObservation.product_variant_id == variant.id)
    ).scalar_one()
    assert count == 1  # no duplicate row
    assert again.id == first.id
    assert again.observed_at == t1  # revalidated in place
    assert again.valid_until is None


def test_quarantined_does_not_replace_last_good(
    db_session: Session, variant: ProductVariant
) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=1)
    t1 = datetime.now(UTC)

    good = record_observation(
        db_session,
        _obs("3.00", observed_at=t0),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t0,
    )
    bad = record_observation(
        db_session,
        _obs("999.00", observed_at=t1),
        product_variant_id=variant.id,
        retailer_id=variant.retailer_id,
        as_of=t1,
        quarantined=True,
        anomaly_type="price_spike",
    )

    # The last-good open row is untouched.
    db_session.refresh(good)
    assert good.valid_until is None
    assert good.amount == Decimal("3.00")

    # The quarantined row is stored closed + disputed, and linked to an anomaly.
    assert bad.valid_until == t1
    assert bad.verification_status == "disputed"
    anomaly = db_session.execute(
        select(PriceAnomaly).where(PriceAnomaly.price_observation_id == bad.id)
    ).scalar_one()
    assert anomaly.status == "quarantined"
    assert anomaly.anomaly_type == "price_spike"
