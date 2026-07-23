"""Cross-source comparison (spec §AB).

When two providers report the same product and scope, compare them — never overwrite one with
the other. Both observations are kept; :class:`PriceResolutionService` selects per policy.
This only classifies the relationship so operators (and anomaly reporting) can see conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct

_MINOR = Decimal("0.02")  # <2% relative gap is consistent
_MATERIAL = Decimal("0.15")  # >=15% is material


class ComparisonState(StrEnum):
    CONSISTENT = "consistent"
    MINOR_DIFFERENCE = "minor_difference"
    MATERIAL_DIFFERENCE = "material_difference"
    INCOMPATIBLE_PACKAGE = "incompatible_package"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    UNRESOLVED_CONFLICT = "unresolved_conflict"


@dataclass(slots=True)
class ComparisonResult:
    state: ComparisonState
    absolute_difference: Decimal | None = None
    percentage_difference: Decimal | None = None
    package_mismatch: bool = False
    availability_mismatch: bool = False
    promotion_mismatch: bool = False
    age_hours_a: float | None = None
    age_hours_b: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "absolute_difference": str(self.absolute_difference)
            if self.absolute_difference is not None
            else None,
            "percentage_difference": str(self.percentage_difference)
            if self.percentage_difference is not None
            else None,
            "package_mismatch": self.package_mismatch,
            "availability_mismatch": self.availability_mismatch,
            "promotion_mismatch": self.promotion_mismatch,
            "age_hours_a": self.age_hours_a,
            "age_hours_b": self.age_hours_b,
        }


def _age(observed_at: datetime, now: datetime) -> float:
    return max(0.0, (now - observed_at).total_seconds() / 3600.0)


def compare_sources(
    a: ExternalCatalogProduct, b: ExternalCatalogProduct, *, now: datetime
) -> ComparisonResult:
    """Classify two same-product observations without merging or overwriting either."""
    package_mismatch = (a.net_content_unit, a.net_content_quantity) != (
        b.net_content_unit,
        b.net_content_quantity,
    )
    availability_mismatch = a.availability is not b.availability
    promotion_mismatch = (a.promotional_price is not None) != (b.promotional_price is not None)
    result = ComparisonResult(
        state=ComparisonState.CONSISTENT,
        package_mismatch=package_mismatch,
        availability_mismatch=availability_mismatch,
        promotion_mismatch=promotion_mismatch,
        age_hours_a=round(_age(a.observed_at, now), 2),
        age_hours_b=round(_age(b.observed_at, now), 2),
    )

    if a.price_scope is not b.price_scope:
        result.state = ComparisonState.INCOMPATIBLE_SCOPE
        return result
    if package_mismatch:
        result.state = ComparisonState.INCOMPATIBLE_PACKAGE
        return result

    low, high = sorted((a.regular_price, b.regular_price))
    result.absolute_difference = high - low
    if low <= 0:
        result.state = (
            ComparisonState.UNRESOLVED_CONFLICT if high > 0 else ComparisonState.CONSISTENT
        )
        return result
    pct = (high - low) / low
    result.percentage_difference = pct.quantize(Decimal("0.0001"))
    if pct < _MINOR:
        result.state = ComparisonState.CONSISTENT
    elif pct < _MATERIAL:
        result.state = ComparisonState.MINOR_DIFFERENCE
    else:
        result.state = ComparisonState.MATERIAL_DIFFERENCE
    return result


__all__ = ["ComparisonResult", "ComparisonState", "compare_sources"]
