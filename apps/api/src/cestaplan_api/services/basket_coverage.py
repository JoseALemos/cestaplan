"""Real basket coverage (spec §X).

Coverage is NOT just a line count. Given each needed basket line's resolved price, this
computes multiple coverages and only labels a basket "coste calculado" (a fully computed
cost) when every condition holds: all needed lines priced, packages identified, quantities
usable, no critical line on an expired price, no un-approved estimate, compatible scope.
Otherwise it reports known vs estimated cost separately, unresolved/old/different-scope
counts, a min-max range, and a global confidence. The phrase "precio exacto" is never used;
the label is one of: coste calculado / coste conocido / coste estimado / coste aproximado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from cestaplan_api.services.price_resolution import FreshnessState, PriceResolution

_PRICED_OK = {FreshnessState.FRESH, FreshnessState.AGING, FreshnessState.STALE}


@dataclass(slots=True)
class BasketLine:
    """One needed line and its resolved price (or None when unresolved)."""

    canonical_name: str
    resolution: PriceResolution | None
    quantity: Decimal = Decimal("1")
    line_cost: Decimal | None = None  # whole-package cost for this line, if computed
    has_package_data: bool = False
    ingredient_mapped: bool = False
    estimate_approved: bool = False  # an estimate for this line was user-approved


@dataclass(slots=True)
class BasketCoverageReport:
    total_lines: int = 0
    line_coverage: float = 0.0
    quantity_coverage: float = 0.0
    estimated_value_coverage: float = 0.0
    fresh_price_coverage: float = 0.0
    exact_scope_coverage: float = 0.0
    package_data_coverage: float = 0.0
    ingredient_mapping_coverage: float = 0.0
    complete: bool = False
    cost_label: str = "coste aproximado"
    cost_known: Decimal = Decimal("0")
    cost_estimated: Decimal = Decimal("0")
    cost_min: Decimal = Decimal("0")
    cost_max: Decimal = Decimal("0")
    unresolved_lines: int = 0
    old_price_lines: int = 0
    different_scope_lines: int = 0
    global_confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_lines": self.total_lines,
            "line_coverage": self.line_coverage,
            "quantity_coverage": self.quantity_coverage,
            "estimated_value_coverage": self.estimated_value_coverage,
            "fresh_price_coverage": self.fresh_price_coverage,
            "exact_scope_coverage": self.exact_scope_coverage,
            "package_data_coverage": self.package_data_coverage,
            "ingredient_mapping_coverage": self.ingredient_mapping_coverage,
            "complete": self.complete,
            "cost_label": self.cost_label,
            "cost_known": str(self.cost_known),
            "cost_estimated": str(self.cost_estimated),
            "cost_min": str(self.cost_min),
            "cost_max": str(self.cost_max),
            "unresolved_lines": self.unresolved_lines,
            "old_price_lines": self.old_price_lines,
            "different_scope_lines": self.different_scope_lines,
            "global_confidence": self.global_confidence,
            "warnings": self.warnings,
        }


def _ratio(count: float, total: float) -> float:
    return round(count / total, 4) if total else 0.0


def _priced(line: BasketLine) -> bool:
    res = line.resolution
    return res is not None and res.selected_price is not None and res.freshness in _PRICED_OK


def evaluate_basket_coverage(lines: list[BasketLine]) -> BasketCoverageReport:
    """Compute the coverages, cost split and honest label for a basket."""
    report = BasketCoverageReport(total_lines=len(lines))
    if not lines:
        report.cost_label = "coste conocido"
        report.complete = True
        return report

    total = len(lines)
    total_qty = sum((line.quantity for line in lines), Decimal("0")) or Decimal("1")
    priced_lines = [line for line in lines if _priced(line)]
    est_used = False
    confidences: list[float] = []

    for line in lines:
        res = line.resolution
        if not _priced(line):
            report.unresolved_lines += 1
            continue
        assert res is not None and res.selected_price is not None
        cost = line.line_cost if line.line_cost is not None else res.selected_price * line.quantity
        if res.price_type == "estimated":
            est_used = True
            report.cost_estimated += cost
            if not line.estimate_approved:
                report.warnings.append(f"{line.canonical_name}: estimate used without approval")
        else:
            report.cost_known += cost
        if res.freshness is not FreshnessState.FRESH:
            report.old_price_lines += 1
        if res.price_scope != "exact_store":
            report.different_scope_lines += 1
        if res.confidence_score is not None:
            confidences.append(float(res.confidence_score))

    report.line_coverage = _ratio(len(priced_lines), total)
    report.quantity_coverage = _ratio(
        float(sum((line.quantity for line in priced_lines), Decimal("0"))), float(total_qty)
    )
    total_value = report.cost_known + report.cost_estimated
    report.estimated_value_coverage = (
        _ratio(float(report.cost_known), float(total_value)) if total_value else 0.0
    )
    report.fresh_price_coverage = _ratio(
        sum(
            1
            for line in priced_lines
            if line.resolution and line.resolution.freshness is FreshnessState.FRESH
        ),
        total,
    )
    report.exact_scope_coverage = _ratio(
        sum(
            1
            for line in priced_lines
            if line.resolution and line.resolution.price_scope == "exact_store"
        ),
        total,
    )
    report.package_data_coverage = _ratio(sum(line.has_package_data for line in lines), total)
    report.ingredient_mapping_coverage = _ratio(
        sum(line.ingredient_mapped for line in lines), total
    )
    report.global_confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0

    # A bad line's true cost is unknown; the basket lies between the known cost (unresolved=0)
    # and the known+estimated upper bound.
    report.cost_min = report.cost_known
    report.cost_max = report.cost_known + report.cost_estimated

    unapproved_estimate = any(
        _priced(line)
        and line.resolution is not None
        and line.resolution.price_type == "estimated"
        and not line.estimate_approved
        for line in lines
    )
    expired_present = any(
        line.resolution is not None and line.resolution.freshness is FreshnessState.EXPIRED
        for line in lines
    )
    report.complete = (
        report.unresolved_lines == 0
        and report.package_data_coverage >= 1.0
        and report.ingredient_mapping_coverage >= 1.0
        and not est_used  # a fully-computed cost uses no estimates (approved or not)
        and not unapproved_estimate
        and not expired_present
    )
    report.cost_label = _label(report, est_used)
    return report


def _label(report: BasketCoverageReport, est_used: bool) -> str:
    if report.complete:
        return "coste calculado"
    if est_used:
        return "coste estimado"
    if report.cost_known > 0:
        return "coste conocido"
    return "coste aproximado"


__all__ = ["BasketCoverageReport", "BasketLine", "evaluate_basket_coverage"]
