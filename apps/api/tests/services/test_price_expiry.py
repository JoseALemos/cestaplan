"""Plan-price expiry derivation: real prices carry no expires_at, so the planner derives
one from the observation date + the plan-price horizon (else a months-old catalogue would
read as 'fresh / complete coverage' forever)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from cestaplan_api.models import Product, ProductPrice
from cestaplan_api.services.planning_context import _package_option


def _price(**over) -> ProductPrice:
    base = {
        "package_quantity": Decimal("500"),
        "package_unit": "g",
        "amount": Decimal("2.00"),
        "unit_price": Decimal("0.40"),
        "availability": "in_stock",
        "source_type": "demo",
        "source_name": "x",
        "confidence_score": Decimal("1.0"),
        "expires_at": None,
    }
    base.update(over)
    return ProductPrice(**base)


def test_derives_expiry_from_observation_when_null():
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    opt = _package_option(Product(id=1, name="p"), _price(observed_at=observed), 45)
    assert opt.expires_at == date(2026, 1, 1) + timedelta(days=45)


def test_keeps_explicit_expiry_when_present():
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    explicit = datetime(2026, 2, 10, tzinfo=UTC)
    opt = _package_option(
        Product(id=1, name="p"), _price(observed_at=observed, expires_at=explicit), 45
    )
    assert opt.expires_at == date(2026, 2, 10)


def test_zero_horizon_disables_derivation():
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    opt = _package_option(Product(id=1, name="p"), _price(observed_at=observed), 0)
    assert opt.expires_at is None


def test_no_observation_leaves_expiry_none():
    opt = _package_option(Product(id=1, name="p"), _price(observed_at=None), 45)
    assert opt.expires_at is None
