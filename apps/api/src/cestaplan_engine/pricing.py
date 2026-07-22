"""Price coverage metrics and status (OPTIMIZATION.md §6).

``price_coverage`` is a simple line ratio; ``weighted_price_coverage`` weights by
economic value so an expensive unpriced line hurts more than a cheap one. The
status maps the metrics to one of six presentation states.
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine.contracts import (
    CoverageCounts,
    CoverageDTO,
    CoverageStatus,
    GroceryLineDTO,
)


def compute_coverage(lines: list[GroceryLineDTO]) -> CoverageDTO:
    """Derive coverage metrics + status from grocery lines."""
    total_lines = len(lines)
    with_price = 0
    without_price = 0
    estimated = 0
    expired = 0
    known_value = Decimal("0")
    estimated_value = Decimal("0")

    for line in lines:
        if line.expired:
            expired += 1
        if line.subtotal_known:
            with_price += 1
            known_value += line.subtotal
        elif line.subtotal > 0:
            # Priced from an estimate / expired value.
            estimated += 1
            estimated_value += line.subtotal
        else:
            # No usable price at all (unmatched or missing).
            without_price += 1

    if total_lines == 0:
        price_coverage = Decimal("1")
    else:
        price_coverage = Decimal(with_price) / Decimal(total_lines)

    total_value = known_value + estimated_value
    if total_value == 0:
        weighted = Decimal("1") if with_price == total_lines else Decimal("0")
    else:
        weighted = known_value / total_value

    status = _status(total_lines, price_coverage, weighted, expired)
    return CoverageDTO(
        price_coverage=price_coverage,
        weighted_price_coverage=weighted,
        status=status,
        counts=CoverageCounts(
            with_price=with_price,
            without_price=without_price,
            estimated=estimated,
            expired=expired,
        ),
    )


def _status(
    total_lines: int,
    price_coverage: Decimal,
    weighted: Decimal,
    expired: int,
) -> CoverageStatus:
    if total_lines == 0:
        return "complete"
    if expired > 0:
        return "stale"
    if price_coverage == 0:
        return "none"
    if price_coverage == 1:
        return "complete"
    if weighted >= Decimal("0.9"):
        return "high"
    if weighted >= Decimal("0.6"):
        return "partial"
    return "insufficient"
