"""Validation of normalized observations (pure, no DB, no network) — spec §11 / §7."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cestaplan_api.ingestion.contracts import (
    AnomalyType,
    NormalizedObservation,
    PriceScope,
    PriceType,
    PromotionInfo,
    PromotionType,
    Severity,
)
from cestaplan_api.ingestion.validation import (
    ObservationValidator,
    ValidationContext,
)

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _obs(**over: object) -> NormalizedObservation:
    base: dict[str, object] = {
        "variant_ref": "EXT-1",
        "amount": Decimal("3.49"),
        "currency": "EUR",
        "price_scope": PriceScope.NATIONAL,
        "price_type": PriceType.REGULAR,
        "observed_at": _NOW - timedelta(hours=1),
        "unit_amount": Decimal("6.98"),
        "unit_code": "kg",
    }
    base.update(over)
    return NormalizedObservation(**base)  # type: ignore[arg-type]


def _ctx(**over: object) -> ValidationContext:
    base: dict[str, object] = {
        "has_store_link": False,
        "now": _NOW,
        "package_quantity": "500",
        "package_unit": "g",
        "package_count": 1,
    }
    base.update(over)
    return ValidationContext(**base)  # type: ignore[arg-type]


def test_valid_observation_passes() -> None:
    result = ObservationValidator().validate(_obs(), _ctx())
    assert result.valid is True
    assert result.errors == ()


def test_amount_must_be_positive() -> None:
    result = ObservationValidator().validate(_obs(amount=Decimal("0")), _ctx())
    assert result.valid is False
    assert result.anomaly_type is AnomalyType.ZERO_OR_NEGATIVE
    assert result.severity is Severity.CRITICAL


def test_unknown_currency_rejected() -> None:
    result = ObservationValidator().validate(_obs(currency="USD"), _ctx())
    assert result.valid is False
    assert any("currency" in e for e in result.errors)


def test_unit_price_incoherence_100x_caught() -> None:
    # amount 3.49 over 500 g is 6.98 €/kg; claiming 698 €/kg is a 100x slip.
    result = ObservationValidator().validate(
        _obs(unit_amount=Decimal("698")), _ctx()
    )
    assert result.valid is False
    assert result.anomaly_type is AnomalyType.UNIT_MISMATCH


def test_unit_price_within_tolerance_ok() -> None:
    result = ObservationValidator().validate(
        _obs(unit_amount=Decimal("6.99")), _ctx()
    )
    assert result.valid is True


def test_missing_external_id_rejected() -> None:
    result = ObservationValidator().validate(_obs(variant_ref=""), _ctx())
    assert result.valid is False
    assert any("variant_ref" in e for e in result.errors)


def test_future_observed_at_rejected() -> None:
    result = ObservationValidator().validate(
        _obs(observed_at=_NOW + timedelta(days=3)), _ctx()
    )
    assert result.valid is False
    assert any("observed_at" in e for e in result.errors)


def test_price_scope_unknown_rejected() -> None:
    result = ObservationValidator().validate(
        _obs(price_scope=PriceScope.UNKNOWN), _ctx()
    )
    assert result.valid is False
    assert any("price_scope" in e for e in result.errors)


def test_exact_store_without_store_link_rejected() -> None:
    result = ObservationValidator().validate(
        _obs(price_scope=PriceScope.EXACT_STORE), _ctx(has_store_link=False)
    )
    assert result.valid is False
    assert any("exact_store" in e for e in result.errors)


def test_exact_store_with_store_link_ok() -> None:
    result = ObservationValidator().validate(
        _obs(price_scope=PriceScope.EXACT_STORE), _ctx(has_store_link=True)
    )
    assert result.valid is True


def test_block_page_input_rejected_and_quarantined() -> None:
    result = ObservationValidator().validate(_obs(), _ctx(is_block_page=True))
    assert result.valid is False
    assert result.anomaly_type is AnomalyType.BLOCK_PAGE
    assert result.severity is Severity.CRITICAL


def test_captcha_login_status_code_rejected() -> None:
    for status in (401, 403, 429, 503):
        result = ObservationValidator().validate(_obs(), _ctx(status_code=status))
        assert result.valid is False
        assert result.anomaly_type is AnomalyType.BLOCK_PAGE


def test_promotion_invalid_date_window_rejected() -> None:
    promo = PromotionInfo(
        promotion_type=PromotionType.NXM,
        required_quantity=2,
        charged_quantity=1,
        valid_from=_NOW,
        valid_until=_NOW - timedelta(days=1),
    )
    result = ObservationValidator().validate(_obs(promotion=promo), _ctx())
    assert result.valid is False
    assert any("promotion" in e for e in result.errors)


def test_structured_report_carries_field_and_severity() -> None:
    report = ObservationValidator().validate_report(_obs(amount=Decimal("-1")), _ctx())
    assert report.valid is False
    err = report.field_errors[0]
    assert err.field == "amount"
    assert err.severity is Severity.CRITICAL
    assert err.anomaly_type is AnomalyType.ZERO_OR_NEGATIVE
