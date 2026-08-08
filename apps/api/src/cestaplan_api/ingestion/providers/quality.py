"""Per-sync provider data-quality metrics (spec §R).

Computes coverage ratios over one sync's :class:`ExternalCatalogProduct` output and grades it
``accepted`` / ``degraded`` / ``insufficient`` / ``quarantined`` against thresholds read from
:class:`~cestaplan_api.config.Settings` (never hard-coded). A provider that scores below the
floors must not be used as a main catalogue; an anomalous drop vs the previous sync is
quarantined so a bad run never replaces good data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct

# --- Per-provider floor policy (spec §R) ----------------------------------- #
# The §R floors in Settings (provider_min_*) are GLOBAL and stay the default for every provider.
# A provider is listed here ONLY when its data model is legitimately different and relaxing a
# floor is documented and defensible — this NEVER relaxes a floor globally.
#
# ``parsebot-dia`` reads the dia.es ONLINE store. That endpoint exposes no net content
# (net_content_quantity/unit stay None — see parsebot/dia.py §7) but DOES publish a real national
# unit price ("0,84 €/l"). DIA products are therefore costed by ``unit_price`` (price-per-unit),
# not by net content, so the net-content ``provider_min_package_coverage`` floor is not a
# meaningful defect for DIA. For providers in this set the package floor is considered satisfied
# when UNIT-PRICE coverage clears the SAME global floor value — i.e. unit-price coverage is
# substituted for net-content coverage against the identical bar, never lowered. A DIA batch that
# also lacked unit prices would still fail the floor. Every other provider keeps net-content
# coverage as the only way to clear the floor.
# ``apify-mercadona`` shares DIA's data model: the Apify actor exposes no net content but does
# publish a real reference unit price ("0.84/L"), so Mercadona products are likewise costed by
# ``unit_price`` (per-unit), not net content. The scoped substitution above applies to it too.
UNIT_PRICE_COSTED_PROVIDERS = frozenset({"parsebot-dia", "apify-mercadona"})


@dataclass(slots=True)
class QualityReport:
    total: int = 0
    identifier_coverage: float = 0.0
    name_coverage: float = 0.0
    price_coverage: float = 0.0
    package_quantity_coverage: float = 0.0
    package_unit_coverage: float = 0.0
    unit_price_coverage: float = 0.0
    barcode_coverage: float = 0.0
    brand_coverage: float = 0.0
    category_coverage: float = 0.0
    store_scope_coverage: float = 0.0
    observed_at_coverage: float = 0.0
    promotion_parse_coverage: float = 0.0
    ingredient_mapping_coverage: float | None = None
    status: str = "insufficient"
    reasons: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def evaluate_quality(
    products: list[ExternalCatalogProduct],
    settings: Settings,
    *,
    previous_count: int | None = None,
    ingredient_mapping_coverage: float | None = None,
) -> QualityReport:
    """Grade one sync's products; ``previous_count`` enables the anomalous-drop check."""
    total = len(products)
    report = QualityReport(total=total, ingredient_mapping_coverage=ingredient_mapping_coverage)
    if total == 0:
        report.status = "quarantined" if previous_count else "insufficient"
        report.reasons = ["empty_catalog"]
        return report

    report.identifier_coverage = _ratio(sum(bool(p.external_product_id) for p in products), total)
    report.name_coverage = _ratio(sum(bool(p.product_name) for p in products), total)
    report.price_coverage = _ratio(sum(p.regular_price > Decimal("0") for p in products), total)
    report.package_quantity_coverage = _ratio(
        sum(p.net_content_quantity is not None for p in products), total
    )
    report.package_unit_coverage = _ratio(
        sum(p.net_content_unit is not None for p in products), total
    )
    report.unit_price_coverage = _ratio(sum(p.unit_price is not None for p in products), total)
    report.barcode_coverage = _ratio(sum(bool(p.barcode) for p in products), total)
    report.brand_coverage = _ratio(sum(bool(p.brand) for p in products), total)
    report.category_coverage = _ratio(sum(bool(p.category) for p in products), total)
    report.store_scope_coverage = _ratio(
        sum(p.price_scope is not PriceScope.UNKNOWN for p in products), total
    )
    report.observed_at_coverage = _ratio(sum(p.observed_at is not None for p in products), total)
    # promotion parse coverage: of products flagged with a promotional price, how many carry a
    # coherent promotion (<= regular). Products with no promotion are counted as fine.
    promo = [p for p in products if p.promotional_price is not None]
    coherent = sum(
        1
        for p in promo
        if p.promotional_price is not None and p.promotional_price <= p.regular_price
    )
    report.promotion_parse_coverage = _ratio(coherent, len(promo)) if promo else 1.0

    # All products in a batch share one provider (set at map time); derive it for the per-provider
    # floor policy. Empty batches returned earlier, so products[0] is safe here.
    provider_code = products[0].provider
    report.reasons = _grade(report, settings, previous_count, provider_code)
    report.status = _status(report, previous_count, settings)
    return report


def _grade(
    report: QualityReport,
    settings: Settings,
    previous_count: int | None,
    provider_code: str,
) -> list[str]:
    reasons: list[str] = []
    if _below(report.price_coverage, settings.provider_min_price_coverage):
        reasons.append("price_coverage_below_floor")
    package_cov = min(report.package_quantity_coverage, report.package_unit_coverage)
    if provider_code in UNIT_PRICE_COSTED_PROVIDERS:
        # DIA & co. are costed by unit price, not net content: unit-price coverage substitutes for
        # net-content coverage against the SAME global floor (never a lowered floor).
        package_cov = max(package_cov, report.unit_price_coverage)
    if _below(package_cov, settings.provider_min_package_coverage):
        reasons.append("package_coverage_below_floor")
    if _below(report.observed_at_coverage, settings.provider_min_observed_at_coverage):
        reasons.append("observed_at_coverage_below_floor")
    if _below(report.barcode_coverage, settings.provider_min_barcode_coverage):
        reasons.append("barcode_coverage_below_floor")
    if report.store_scope_coverage <= 0:
        reasons.append("geographic_scope_undeterminable")
    if (
        previous_count
        and previous_count > 0
        and settings.provider_max_catalog_drop_ratio > 0
        and report.total < previous_count * (1 - settings.provider_max_catalog_drop_ratio)
    ):
        reasons.append("anomalous_catalog_drop")
    return reasons


def _status(report: QualityReport, previous_count: int | None, settings: Settings) -> str:
    reasons = report.reasons or []
    if "anomalous_catalog_drop" in reasons:
        return "quarantined"
    hard = {
        "price_coverage_below_floor",
        "package_coverage_below_floor",
        "observed_at_coverage_below_floor",
        "geographic_scope_undeterminable",
    }
    if hard & set(reasons):
        return "insufficient"
    if reasons:
        return "degraded"
    return "accepted"


def _below(value: float, floor: float) -> bool:
    return floor > 0 and value < floor


__all__ = ["UNIT_PRICE_COSTED_PROVIDERS", "QualityReport", "evaluate_quality"]
