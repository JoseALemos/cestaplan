"""ORM invariants for the price-ingestion models (live Postgres, no network).

Covers: PriceObservation is append-only history, idempotency unique constraints hold,
and the VARCHAR+CHECK enum columns reject invalid values.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from cestaplan_api.models import ExternalProduct, PriceObservation, ProductVariant


def _make_observation(
    variant: ProductVariant, *, amount: str, valid_from: datetime
) -> PriceObservation:
    now = datetime.now(UTC)
    return PriceObservation(
        retailer_id=variant.retailer_id,
        product_variant_id=variant.id,
        price_scope="national",
        price_type="regular",
        amount=Decimal(amount),
        currency="EUR",
        observed_at=now,
        imported_at=now,
        valid_from=valid_from,
        confidence_score=Decimal("1.0"),
    )


def test_price_observation_is_append_only(db_session: Session, variant: ProductVariant) -> None:
    """Closing the prior row's valid_until + inserting a new row builds history."""
    t0 = datetime.now(UTC) - timedelta(days=1)
    t1 = datetime.now(UTC)

    first = _make_observation(variant, amount="1.50", valid_from=t0)
    db_session.add(first)
    db_session.flush()

    # Close the open interval (this is the only mutation allowed) and append a new row.
    first.valid_until = t1
    second = _make_observation(variant, amount="1.75", valid_from=t1)
    db_session.add(second)
    db_session.flush()

    rows = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.product_variant_id == variant.id)
        .order_by(PriceObservation.valid_from)
    ).scalars().all()

    assert len(rows) == 2
    assert rows[0].valid_until == t1  # prior interval closed
    assert rows[0].amount == Decimal("1.5000")
    assert rows[1].valid_until is None  # current interval open
    assert rows[1].amount == Decimal("1.7500")


def test_external_product_unique_constraint(db_session: Session, variant: ProductVariant) -> None:
    """(retailer_id, external_id) is unique -> a duplicate insert is rejected (idempotency)."""
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            ExternalProduct(retailer_id=variant.retailer_id, external_id="EXT-1")
        )
        db_session.flush()


def test_connector_state_scope_unique(db_session: Session, variant: ProductVariant) -> None:
    """(retailer_id, store_id, connector_version) is unique for ConnectorState.

    Uses a concrete store_id: Postgres treats NULLs as distinct in a unique index, so the
    idempotency guarantee is only meaningful for a non-null scope.
    """
    from cestaplan_api.models import ConnectorState, Store

    store = db_session.execute(
        select(Store).where(Store.retailer_id == variant.retailer_id)
    ).scalars().first()
    assert store is not None

    common = {
        "retailer_id": variant.retailer_id,
        "store_id": store.id,
        "connector_version": "1.0.0",
        "parser_version": "1.0.0",
    }
    db_session.add(ConnectorState(**common))
    db_session.flush()

    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(ConnectorState(**common))
        db_session.flush()


def test_enum_check_rejects_invalid_value(db_session: Session, variant: ProductVariant) -> None:
    """The VARCHAR+CHECK price_scope column only accepts declared enum values."""
    bad = _make_observation(variant, amount="2.00", valid_from=datetime.now(UTC))
    bad.price_scope = "galaxy"  # not a valid PriceScope value
    db_session.add(bad)
    with pytest.raises((IntegrityError, StatementError)), db_session.begin_nested():
        db_session.flush()


def test_price_observation_defaults(db_session: Session, variant: ProductVariant) -> None:
    """Server defaults populate verification_status/requires_loyalty on insert."""
    obs = _make_observation(variant, amount="3.00", valid_from=datetime.now(UTC))
    db_session.add(obs)
    db_session.flush()
    db_session.refresh(obs)
    assert obs.verification_status == "unverified"
    assert obs.requires_loyalty is False
    count = db_session.scalar(
        select(func.count()).select_from(PriceObservation).where(
            PriceObservation.id == obs.id
        )
    )
    assert count == 1
