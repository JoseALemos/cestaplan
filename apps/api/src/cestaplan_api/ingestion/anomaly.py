"""Batch-level anomaly detection for price ingestion (spec §11).

Pure logic, no DB and no network. :class:`AnomalyDetector` compares a freshly parsed
:class:`Batch` against caller-supplied :class:`PriorStats` (the last-good snapshot) and
flags conditions that mean "do not trust this run": catalog collapse or impossible
growth, extreme price moves, x100 / /100 slips, unit or package changes, uniform
pricing, empty catalogs, block-page HTML, a parser that returned nothing, currency
mismatches, and coverage far below the previous day.

Every finding recommends **quarantine** — the pipeline never auto-replaces last-good
data with a suspect batch. Thresholds are constructor arguments with sane defaults.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from cestaplan_api.ingestion.contracts import (
    AnomalyType,
    NormalizedObservation,
    Severity,
)

# Catalog-level anomaly kinds that have no dedicated enum member. The contract keeps
# ``anomaly_type`` free-text on purpose, so detectors may introduce new kinds without a
# migration; these are the canonical string values this detector emits.
CATALOG_DROP = "catalog_drop"
CATALOG_GROWTH = "catalog_growth"
ALL_SAME_PRICE = "all_same_price"
EMPTY_CATALOG = "empty_catalog"
PARSER_ZERO = "parser_returned_zero"
PACKAGE_CHANGE = "package_change"
COVERAGE_DROP = "coverage_drop"
PRICE_X100 = "price_x100"

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class Anomaly:
    """A single detected anomaly. ``anomaly_type`` is an :class:`AnomalyType` when one
    fits, or a canonical free-text kind (module constants) for catalog-level findings."""

    anomaly_type: AnomalyType | str
    severity: Severity
    expected: object | None = None
    actual: object | None = None
    details: dict[str, object] = field(default_factory=dict)
    recommended_action: str = QUARANTINE
    variant_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PriorStats:
    """Last-good snapshot the incoming batch is compared against."""

    catalog_size: int = 0
    prices: Mapping[str, Decimal] = field(default_factory=dict)
    units: Mapping[str, str] = field(default_factory=dict)
    packages: Mapping[str, Decimal] = field(default_factory=dict)
    external_products: Mapping[str, str] = field(default_factory=dict)
    currency: str = "EUR"
    coverage: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Batch:
    """A freshly parsed batch plus the source signals needed to judge it."""

    observations: tuple[NormalizedObservation, ...] = ()
    is_block_page: bool = False
    parser_returned_zero: bool = False
    coverage: Decimal | None = None
    packages: Mapping[str, Decimal] = field(default_factory=dict)
    external_products: Mapping[str, str] = field(default_factory=dict)


class AnomalyDetector:
    """Flags untrustworthy ingestion batches. All thresholds are configurable."""

    def __init__(
        self,
        *,
        catalog_drop_threshold: Decimal = Decimal("0.9"),
        catalog_growth_factor: Decimal = Decimal("10"),
        extreme_price_change_factor: Decimal = Decimal("3"),
        price_x100_tolerance: Decimal = Decimal("0.05"),
        same_price_min_products: int = 5,
        same_price_fraction: Decimal = Decimal("0.95"),
        coverage_drop_threshold: Decimal = Decimal("0.3"),
        quarantine_severity: Severity = Severity.HIGH,
    ) -> None:
        self.catalog_drop_threshold = catalog_drop_threshold
        self.catalog_growth_factor = catalog_growth_factor
        self.extreme_price_change_factor = extreme_price_change_factor
        self.price_x100_tolerance = price_x100_tolerance
        self.same_price_min_products = same_price_min_products
        self.same_price_fraction = same_price_fraction
        self.coverage_drop_threshold = coverage_drop_threshold
        self.quarantine_severity = quarantine_severity

    def detect(self, batch: Batch, prior: PriorStats | None = None) -> list[Anomaly]:
        prior = prior or PriorStats()
        anomalies: list[Anomaly] = []

        # -- source-level signals -------------------------------------------- #
        if batch.is_block_page:
            anomalies.append(
                Anomaly(AnomalyType.BLOCK_PAGE, Severity.CRITICAL,
                        details={"reason": "block/login/CAPTCHA page returned"})
            )
        if batch.parser_returned_zero:
            anomalies.append(
                Anomaly(PARSER_ZERO, Severity.HIGH,
                        details={"reason": "parser produced zero observations"})
            )

        size = len(batch.observations)
        if size == 0 and not batch.parser_returned_zero:
            anomalies.append(
                Anomaly(EMPTY_CATALOG, Severity.HIGH, expected=prior.catalog_size,
                        actual=0, details={"reason": "empty catalog"})
            )

        # -- catalog size -------------------------------------------------- #
        anomalies.extend(self._catalog_size(size, prior))

        # -- uniform pricing ----------------------------------------------- #
        same = self._all_same_price(batch)
        if same is not None:
            anomalies.append(same)

        # -- currency ------------------------------------------------------ #
        anomalies.extend(self._currency(batch, prior))

        # -- per-observation comparisons ----------------------------------- #
        anomalies.extend(self._per_observation(batch, prior))

        # -- coverage ------------------------------------------------------ #
        cov = self._coverage(batch, prior)
        if cov is not None:
            anomalies.append(cov)

        return anomalies

    def should_quarantine(self, anomalies: list[Anomaly]) -> bool:
        """Whether the batch must be quarantined rather than promoted to last-good."""
        threshold = _SEVERITY_RANK[self.quarantine_severity]
        return any(_SEVERITY_RANK[a.severity] >= threshold for a in anomalies)

    # ------------------------------------------------------------------ #
    # Individual checks
    # ------------------------------------------------------------------ #

    def _catalog_size(self, size: int, prior: PriorStats) -> list[Anomaly]:
        out: list[Anomaly] = []
        if prior.catalog_size <= 0:
            return out
        drop = Decimal(prior.catalog_size - size) / Decimal(prior.catalog_size)
        if size > 0 and drop >= self.catalog_drop_threshold:
            out.append(
                Anomaly(CATALOG_DROP, Severity.CRITICAL, expected=prior.catalog_size,
                        actual=size, details={"drop_fraction": drop})
            )
        if Decimal(size) > Decimal(prior.catalog_size) * self.catalog_growth_factor:
            out.append(
                Anomaly(CATALOG_GROWTH, Severity.HIGH, expected=prior.catalog_size,
                        actual=size,
                        details={"growth_factor": Decimal(size) / Decimal(prior.catalog_size)})
            )
        return out

    def _all_same_price(self, batch: Batch) -> Anomaly | None:
        amounts = [o.amount for o in batch.observations if o.amount is not None]
        if len(amounts) < self.same_price_min_products:
            return None
        counts = Counter(amounts)
        top_price, top_count = counts.most_common(1)[0]
        fraction = Decimal(top_count) / Decimal(len(amounts))
        if fraction >= self.same_price_fraction:
            return Anomaly(
                ALL_SAME_PRICE, Severity.HIGH, actual=top_price,
                details={"fraction": fraction, "price": top_price},
            )
        return None

    def _currency(self, batch: Batch, prior: PriorStats) -> list[Anomaly]:
        out: list[Anomaly] = []
        expected = prior.currency.upper()
        for o in batch.observations:
            if o.currency and o.currency.upper() != expected:
                out.append(
                    Anomaly(AnomalyType.CURRENCY_MISMATCH, Severity.HIGH,
                            expected=expected, actual=o.currency,
                            variant_ref=o.variant_ref,
                            details={"reason": "currency differs from prior"})
                )
        return out

    def _per_observation(self, batch: Batch, prior: PriorStats) -> list[Anomaly]:
        out: list[Anomaly] = []
        for o in batch.observations:
            ref = o.variant_ref

            if o.amount is not None and o.amount <= 0:
                out.append(
                    Anomaly(AnomalyType.ZERO_OR_NEGATIVE, Severity.CRITICAL,
                            actual=o.amount, variant_ref=ref)
                )

            prior_price = prior.prices.get(ref)
            if prior_price is not None and prior_price > 0 and o.amount is not None:
                out.extend(self._price_move(ref, prior_price, o.amount))

            prior_unit = prior.units.get(ref)
            if prior_unit is not None and o.unit_code is not None and (
                prior_unit != o.unit_code
            ):
                out.append(
                    Anomaly(AnomalyType.UNIT_MISMATCH, Severity.HIGH,
                            expected=prior_unit, actual=o.unit_code, variant_ref=ref,
                            details={"reason": "unit code changed"})
                )

            prior_pkg = prior.packages.get(ref)
            new_pkg = batch.packages.get(ref)
            if prior_pkg is not None and new_pkg is not None and prior_pkg != new_pkg:
                same_product = (
                    prior.external_products.get(ref) == batch.external_products.get(ref)
                    and ref in prior.external_products
                )
                if same_product:
                    out.append(
                        Anomaly(PACKAGE_CHANGE, Severity.HIGH, expected=prior_pkg,
                                actual=new_pkg, variant_ref=ref,
                                details={"reason": "package changed without product change"})
                    )
        return out

    def _price_move(self, ref: str, old: Decimal, new: Decimal) -> list[Anomaly]:
        ratio = new / old
        # x100 / /100 slips first (a superset of "extreme"); report the specific kind.
        if abs(ratio - Decimal("100")) <= Decimal("100") * self.price_x100_tolerance:
            return [Anomaly(AnomalyType.PRICE_SPIKE, Severity.CRITICAL, expected=old,
                            actual=new, variant_ref=ref,
                            details={"kind": PRICE_X100, "ratio": ratio})]
        if abs(ratio - Decimal("0.01")) <= Decimal("0.01") * self.price_x100_tolerance:
            return [Anomaly(AnomalyType.PRICE_DROP, Severity.CRITICAL, expected=old,
                            actual=new, variant_ref=ref,
                            details={"kind": PRICE_X100, "ratio": ratio})]
        if ratio >= self.extreme_price_change_factor:
            return [Anomaly(AnomalyType.PRICE_SPIKE, Severity.HIGH, expected=old,
                            actual=new, variant_ref=ref, details={"ratio": ratio})]
        if ratio <= Decimal("1") / self.extreme_price_change_factor:
            return [Anomaly(AnomalyType.PRICE_DROP, Severity.HIGH, expected=old,
                            actual=new, variant_ref=ref, details={"ratio": ratio})]
        return []

    def _coverage(self, batch: Batch, prior: PriorStats) -> Anomaly | None:
        if prior.coverage is None or batch.coverage is None:
            return None
        if prior.coverage - batch.coverage >= self.coverage_drop_threshold:
            return Anomaly(
                COVERAGE_DROP, Severity.HIGH, expected=prior.coverage,
                actual=batch.coverage,
                details={"drop": prior.coverage - batch.coverage},
            )
        return None


__all__ = [
    "ALL_SAME_PRICE",
    "CATALOG_DROP",
    "CATALOG_GROWTH",
    "COVERAGE_DROP",
    "EMPTY_CATALOG",
    "PACKAGE_CHANGE",
    "PARSER_ZERO",
    "PRICE_X100",
    "QUARANTINE",
    "Anomaly",
    "AnomalyDetector",
    "Batch",
    "PriorStats",
]
