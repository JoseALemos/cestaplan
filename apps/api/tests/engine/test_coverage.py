"""Price-coverage formulas and the six status thresholds (OPTIMIZATION.md §6)."""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine.contracts import GroceryLineDTO
from cestaplan_engine.pricing import compute_coverage


def line(
    name: str,
    subtotal: str,
    *,
    known: bool,
    expired: bool = False,
) -> GroceryLineDTO:
    return GroceryLineDTO(
        canonical_name=name,
        product_id=name,
        display_name=name,
        needed_quantity=Decimal("1"),
        subtotal=Decimal(subtotal),
        subtotal_known=known,
        expired=expired,
    )


def test_price_coverage_simple_ratio():
    lines = [
        line("a", "10", known=True),
        line("b", "10", known=True),
        line("c", "0", known=False),
        line("d", "0", known=False),
    ]
    cov = compute_coverage(lines)
    assert cov.price_coverage == Decimal("2") / Decimal("4")
    assert cov.counts.with_price == 2
    assert cov.counts.without_price == 2


def test_weighted_coverage_formula():
    lines = [
        line("expensive", "95", known=True),
        line("cheap_estimate", "5", known=False),
    ]
    cov = compute_coverage(lines)
    # weighted = known_value / (known + estimated) = 95 / 100
    assert cov.weighted_price_coverage == Decimal("95") / Decimal("100")


def test_status_complete():
    cov = compute_coverage([line("a", "10", known=True), line("b", "5", known=True)])
    assert cov.status == "complete"
    assert cov.price_coverage == Decimal("1")


def test_status_high():
    cov = compute_coverage(
        [line("a", "95", known=True), line("b", "5", known=False)]
    )
    assert cov.weighted_price_coverage >= Decimal("0.9")
    assert cov.status == "high"


def test_status_partial():
    cov = compute_coverage(
        [line("a", "70", known=True), line("b", "30", known=False)]
    )
    assert cov.status == "partial"


def test_status_insufficient():
    cov = compute_coverage(
        [line("a", "50", known=True), line("b", "50", known=False)]
    )
    assert cov.status == "insufficient"


def test_status_none():
    cov = compute_coverage([line("a", "0", known=False), line("b", "0", known=False)])
    assert cov.price_coverage == Decimal("0")
    assert cov.status == "none"


def test_status_stale_when_expired_present():
    cov = compute_coverage(
        [line("a", "10", known=True), line("b", "5", known=False, expired=True)]
    )
    assert cov.counts.expired == 1
    assert cov.status == "stale"


def test_empty_is_complete():
    cov = compute_coverage([])
    assert cov.status == "complete"
    assert cov.price_coverage == Decimal("1")
