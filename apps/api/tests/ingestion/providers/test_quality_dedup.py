"""Provider quality grading (§R) and duplicate report (§Q) — pure logic, no DB.

Quality: accepted when floors are met, insufficient on a hard-floor miss, quarantined on an
anomalous drop. Dedup: never merges; clusters are 'review' or 'do_not_merge' (different
retailer / distinguishing variant / different size), and name matches are always manual.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import (
    ContentUnit,
    ExternalCatalogProduct,
    SellUnit,
)
from cestaplan_api.ingestion.providers.dedup import find_duplicate_candidates
from cestaplan_api.ingestion.providers.quality import evaluate_quality

_NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "provider_min_price_coverage": 0.95,
        "provider_min_package_coverage": 0.80,
        "provider_min_observed_at_coverage": 0.95,
        "provider_min_barcode_coverage": 0.0,
        "provider_max_catalog_drop_ratio": 0.50,
    }
    base.update(over)
    return Settings(**base)


def _p(external_id: str, **over: Any) -> ExternalCatalogProduct:
    kw: dict[str, Any] = {
        "provider": "demo",
        "retailer_slug": "r1",
        "external_product_id": external_id,
        "product_name": "Leche entera 1 L",
        "sell_unit": SellUnit.PACKAGE,
        "regular_price": Decimal("0.88"),
        "currency": "EUR",
        "price_scope": PriceScope.NATIONAL,
        "observed_at": _NOW,
        "net_content_quantity": Decimal("1000"),
        "net_content_unit": ContentUnit.ML,
        "barcode": None,
    }
    kw.update(over)
    return ExternalCatalogProduct(**kw)


# --- quality --------------------------------------------------------------- #
def test_quality_accepted_when_floors_met() -> None:
    products = [_p(f"A{i}") for i in range(5)]
    report = evaluate_quality(products, _settings())
    assert report.status == "accepted"
    assert report.price_coverage == 1.0
    assert report.package_unit_coverage == 1.0


def test_quality_insufficient_on_low_price_coverage() -> None:
    products = [_p("A1", regular_price=Decimal("0")), _p("A2"), _p("A3")]
    report = evaluate_quality(products, _settings())
    assert report.status == "insufficient"
    assert "price_coverage_below_floor" in (report.reasons or [])


def test_quality_insufficient_on_missing_package() -> None:
    products = [_p("A1", net_content_unit=None, net_content_quantity=None), _p("A2"), _p("A3")]
    report = evaluate_quality(products, _settings())
    assert report.status == "insufficient"
    assert "package_coverage_below_floor" in (report.reasons or [])


def test_quality_quarantined_on_catalog_drop() -> None:
    products = [_p("A1"), _p("A2")]  # only 2 now...
    report = evaluate_quality(products, _settings(), previous_count=100)  # ...vs 100 before
    assert report.status == "quarantined"
    assert "anomalous_catalog_drop" in (report.reasons or [])


def test_quality_empty_is_quarantined_when_had_previous() -> None:
    assert evaluate_quality([], _settings(), previous_count=10).status == "quarantined"
    assert evaluate_quality([], _settings()).status == "insufficient"


# --- dedup ----------------------------------------------------------------- #
def test_same_barcode_same_everything_is_review() -> None:
    clusters = find_duplicate_candidates(
        [_p("A1", barcode="84100001"), _p("A2", barcode="84100001")]
    )
    assert len(clusters) == 1
    assert clusters[0].basis == "barcode"
    assert clusters[0].recommendation == "review"


def test_same_barcode_different_retailer_do_not_merge() -> None:
    clusters = find_duplicate_candidates(
        [_p("A1", barcode="84100002"), _p("A2", barcode="84100002", retailer_slug="r2")]
    )
    assert clusters[0].recommendation == "do_not_merge"
    assert clusters[0].reason == "different_retailers"


def test_same_barcode_distinguishing_variant_do_not_merge() -> None:
    clusters = find_duplicate_candidates(
        [
            _p("A1", barcode="84100003", product_name="Leche entera 1 L"),
            _p("A2", barcode="84100003", product_name="Leche desnatada 1 L"),
        ]
    )
    assert clusters[0].recommendation == "do_not_merge"
    assert clusters[0].reason == "distinguishing_variant"


def test_same_barcode_different_size_do_not_merge() -> None:
    clusters = find_duplicate_candidates(
        [
            _p("A1", barcode="84100004"),
            _p("A2", barcode="84100004", net_content_quantity=Decimal("500")),
        ]
    )
    assert clusters[0].recommendation == "do_not_merge"
    assert clusters[0].reason == "different_size"


def test_name_match_without_barcode_is_manual_review() -> None:
    # Same normalized name + same net content -> a name-only match is ALWAYS manual review,
    # never an automatic merge.
    clusters = find_duplicate_candidates(
        [_p("A1", product_name="Tomate frito 400 g"), _p("A2", product_name="Tomate frito 800 g")]
    )
    assert len(clusters) == 1
    assert clusters[0].basis == "normalized_name"
    assert clusters[0].recommendation == "review"
    assert clusters[0].reason == "name_match_manual_review"


def test_name_match_different_size_do_not_merge() -> None:
    clusters = find_duplicate_candidates(
        [
            _p("A1", product_name="Tomate frito"),
            _p("A2", product_name="Tomate frito", net_content_quantity=Decimal("500")),
        ]
    )
    assert clusters[0].basis == "normalized_name"
    assert clusters[0].recommendation == "do_not_merge"
    assert clusters[0].reason == "different_size"


def test_no_duplicates_reports_nothing() -> None:
    assert find_duplicate_candidates([_p("A1"), _p("B1", product_name="Pan de molde")]) == []
