"""Batch anomaly detection (pure, no DB, no network) — spec §11."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion.anomaly import (
    ALL_SAME_PRICE,
    CATALOG_DROP,
    CATALOG_GROWTH,
    COVERAGE_DROP,
    EMPTY_CATALOG,
    PACKAGE_CHANGE,
    PARSER_ZERO,
    PRICE_X100,
    Anomaly,
    AnomalyDetector,
    Batch,
    PriorStats,
)
from cestaplan_api.ingestion.contracts import (
    AnomalyType,
    NormalizedObservation,
    PriceScope,
    PriceType,
    Severity,
)

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _obs(
    ref: str, amount: str, *, currency: str = "EUR", unit_code: str = "kg"
) -> NormalizedObservation:
    return NormalizedObservation(
        variant_ref=ref,
        amount=Decimal(amount),
        currency=currency,
        price_scope=PriceScope.NATIONAL,
        price_type=PriceType.REGULAR,
        observed_at=_NOW,
        unit_code=unit_code,
    )


def _batch(refs_amounts: list[tuple[str, str]], **over: object) -> Batch:
    obs = tuple(_obs(r, a) for r, a in refs_amounts)
    return Batch(observations=obs, **over)  # type: ignore[arg-type]


def _types(anomalies: list[Anomaly]) -> set[object]:
    return {a.anomaly_type for a in anomalies}


def test_normal_small_change_no_anomaly() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=3, prices={"A": Decimal("1.50")}, currency="EUR")
    batch = _batch([("A", "1.60"), ("B", "2.00"), ("C", "3.00")])
    anomalies = det.detect(batch, prior)
    assert anomalies == []
    assert det.should_quarantine(anomalies) is False


def test_catalog_drop_90pct_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=100)
    batch = _batch([("A", "1.00"), ("B", "2.00")])  # 2 of 100 => 98% drop
    anomalies = det.detect(batch, prior)
    assert CATALOG_DROP in _types(anomalies)
    hit = next(a for a in anomalies if a.anomaly_type == CATALOG_DROP)
    assert hit.severity is Severity.CRITICAL
    assert hit.recommended_action == "quarantine"
    assert det.should_quarantine(anomalies) is True


def test_impossible_catalog_growth_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=2)
    batch = _batch([(f"P{i}", "1.00") for i in range(40)])  # 40 vs 2 => 20x
    anomalies = det.detect(batch, prior)
    assert CATALOG_GROWTH in _types(anomalies)


def test_empty_catalog_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=50)
    anomalies = det.detect(Batch(observations=()), prior)
    assert EMPTY_CATALOG in _types(anomalies)
    assert det.should_quarantine(anomalies) is True


def test_price_x100_flagged_critical() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1, prices={"A": Decimal("2.50")})
    batch = _batch([("A", "250.00")])  # x100 slip
    anomalies = det.detect(batch, prior)
    spike = next(a for a in anomalies if a.anomaly_type is AnomalyType.PRICE_SPIKE)
    assert spike.severity is Severity.CRITICAL
    assert spike.details.get("kind") == PRICE_X100
    assert det.should_quarantine(anomalies) is True


def test_price_divide_100_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1, prices={"A": Decimal("250.00")})
    batch = _batch([("A", "2.50")])  # /100 slip
    anomalies = det.detect(batch, prior)
    drop = next(a for a in anomalies if a.anomaly_type is AnomalyType.PRICE_DROP)
    assert drop.details.get("kind") == PRICE_X100


def test_extreme_price_change_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1, prices={"A": Decimal("2.00")})
    batch = _batch([("A", "10.00")])  # 5x, not 100x
    anomalies = det.detect(batch, prior)
    assert AnomalyType.PRICE_SPIKE in _types(anomalies)


def test_all_products_same_price_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=6)
    batch = _batch([(f"P{i}", "1.00") for i in range(6)])
    anomalies = det.detect(batch, prior)
    assert ALL_SAME_PRICE in _types(anomalies)
    assert det.should_quarantine(anomalies) is True


def test_block_page_flagged_critical() -> None:
    det = AnomalyDetector()
    anomalies = det.detect(Batch(observations=(), is_block_page=True))
    assert AnomalyType.BLOCK_PAGE in _types(anomalies)
    assert det.should_quarantine(anomalies) is True


def test_parser_returned_zero_flagged() -> None:
    det = AnomalyDetector()
    anomalies = det.detect(Batch(observations=(), parser_returned_zero=True))
    assert PARSER_ZERO in _types(anomalies)
    # parser_returned_zero suppresses the separate empty-catalog finding.
    assert EMPTY_CATALOG not in _types(anomalies)


def test_currency_mismatch_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1, currency="EUR")
    batch = Batch(observations=(_obs("A", "1.00", currency="USD"),))
    anomalies = det.detect(batch, prior)
    assert AnomalyType.CURRENCY_MISMATCH in _types(anomalies)


def test_unit_change_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1, units={"A": "kg"})
    batch = Batch(observations=(_obs("A", "1.00", unit_code="l"),))
    anomalies = det.detect(batch, prior)
    assert AnomalyType.UNIT_MISMATCH in _types(anomalies)


def test_package_change_without_product_change_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(
        catalog_size=1,
        packages={"A": Decimal("0.5")},
        external_products={"A": "EXT-100"},
    )
    batch = Batch(
        observations=(_obs("A", "1.00"),),
        packages={"A": Decimal("1.0")},
        external_products={"A": "EXT-100"},  # same product, different package
    )
    anomalies = det.detect(batch, prior)
    assert PACKAGE_CHANGE in _types(anomalies)


def test_coverage_far_below_previous_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=3, coverage=Decimal("0.95"))
    batch = _batch([("A", "1.0"), ("B", "2.0"), ("C", "3.0")], coverage=Decimal("0.40"))
    anomalies = det.detect(batch, prior)
    assert COVERAGE_DROP in _types(anomalies)


def test_zero_or_negative_price_flagged() -> None:
    det = AnomalyDetector()
    prior = PriorStats(catalog_size=1)
    batch = _batch([("A", "0.00"), ("B", "1.0"), ("C", "2.0")])
    anomalies = det.detect(batch, prior)
    assert AnomalyType.ZERO_OR_NEGATIVE in _types(anomalies)


def test_thresholds_are_configurable() -> None:
    # A stricter extreme-change factor flags a move a laxer detector ignores.
    prior = PriorStats(catalog_size=1, prices={"A": Decimal("2.00")})
    batch = _batch([("A", "3.60")])  # 1.8x
    assert AnomalyDetector().detect(batch, prior) == []
    strict = AnomalyDetector(extreme_price_change_factor=Decimal("1.5"))
    assert AnomalyType.PRICE_SPIKE in _types(strict.detect(batch, prior))
