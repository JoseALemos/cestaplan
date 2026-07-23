"""Retailer onboarding matrix (spec §1) — declares the seven chains + their roles/rights.

Pure data + small helpers. Declares each provider's intended role, catalogue scope, roll-out
stage, declared capabilities and rights, and computes an honest per-provider status from the
configuration actually present (credentials/base URLs) — a chain is never marked ready without
a real capture. Nothing here activates production or shows a secret.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct
from cestaplan_api.models import ProviderActivation

# A capture must clear these fractions (price + package quantity + unit) to be costable.
_COSTING_THRESHOLD = Decimal("0.8")


@dataclass(frozen=True, slots=True)
class MatrixEntry:
    provider_code: str
    retailer_slug: str
    intended_role: str
    # DECLARED intent only — never evidence of observed coverage. full | partial | complementary
    intended_catalog_scope: str
    activation_state: str  # disabled | transport_only | shadow | ...
    capabilities: tuple[str, ...] = ()
    rights: str = "under_review"
    needs_credentials: bool = False  # apify/parsebot need a key
    needs_base_url: bool = False  # parse.bot chains need a scraper base URL
    authorized_feed_required: bool = False


# The seven initial chains + the complementary/demo sources (spec §1).
RETAILER_MATRIX: tuple[MatrixEntry, ...] = (
    MatrixEntry(
        "parsebot-dia",
        "dia",
        "dense_catalog",
        "full",
        "transport_only",
        ("products", "categories", "search", "product_details", "prices", "promotions"),
        needs_credentials=True,
        needs_base_url=True,
    ),
    MatrixEntry(
        "parsebot-alcampo",
        "alcampo",
        "dense_catalog",
        "full",
        "transport_only",
        ("products", "categories", "search", "product_details", "prices", "promotions", "stores"),
        needs_credentials=True,
        needs_base_url=True,
    ),
    MatrixEntry(
        "apify-mercadona",
        "mercadona",
        "dense_catalog_experimental",
        "full",
        "disabled",
        ("products", "prices"),
        needs_credentials=True,
    ),
    MatrixEntry(
        "parsebot-carrefour",
        "carrefour",
        "dense_catalog_candidate",
        "full",
        "disabled",
        ("products", "prices"),
        needs_credentials=True,
        needs_base_url=True,
    ),
    MatrixEntry(
        "parsebot-lidl",
        "lidl",
        "partial_offers",
        "partial",
        "disabled",
        ("promotions",),
        needs_credentials=True,
        needs_base_url=True,
    ),
    MatrixEntry(
        "parsebot-aldi",
        "aldi",
        "partial_offers",
        "partial",
        "disabled",
        ("promotions",),
        needs_credentials=True,
        needs_base_url=True,
    ),
    MatrixEntry(
        "parsebot-deza",
        "deza",
        "partial_offers",
        "partial",
        "disabled",
        ("promotions",),
        needs_credentials=True,
        needs_base_url=True,
        authorized_feed_required=True,
    ),
    MatrixEntry(
        "open-prices",
        "open_prices",
        "complementary",
        "complementary",
        "shadow",
        ("prices",),
        rights="community_review",
    ),
    MatrixEntry(
        "demo",
        "mercaejemplo",
        "development_fallback",
        "complementary",
        "disabled",
        ("products", "prices"),
        rights="own",
    ),
)

_MATRIX_BY_CODE = {e.provider_code: e for e in RETAILER_MATRIX}


@dataclass(slots=True)
class CoverageObservation:
    """Coverage MEASURED from a real (bounded) capture — never declared from intent."""

    observed_catalog_scope: str = "unknown"  # unknown | sample_only | partial | full
    price_coverage: Decimal | None = None
    package_quantity_coverage: Decimal | None = None
    package_unit_coverage: Decimal | None = None
    geographic_scope_coverage: Decimal | None = None
    costing_eligibility: str = "unknown"  # unknown | insufficient | sufficient


def _ratio(n: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return (Decimal(n) / Decimal(total)).quantize(Decimal("0.0001"))


def _costing_eligibility(obs: CoverageObservation) -> str:
    """Costable only with a real observed scope AND high price/package coverage.

    A ``sample_only``/``unknown`` scope is never costable — a handful of records cannot prove
    catalogue breadth. Geographic coverage is deliberately excluded here (it gates localisation,
    not whether a recipe can be priced).
    """
    if obs.observed_catalog_scope not in ("full", "partial"):
        return "insufficient"
    needed = (obs.price_coverage, obs.package_quantity_coverage, obs.package_unit_coverage)
    if any(v is None or v < _COSTING_THRESHOLD for v in needed):
        return "insufficient"
    return "sufficient"


def measure_coverage(
    products: Sequence[ExternalCatalogProduct],
    *,
    captured: int,
    limit: int,
    supports_full_catalog: bool,
    supports_store_scope: bool,
) -> CoverageObservation:
    """Derive observed coverage + costing eligibility from a real capture (not from intent).

    Hitting the capture limit, or a source without full-catalogue support, means ``sample_only``:
    breadth is unproven. Package-content coverage (net quantity/unit) is what makes a price
    costable for a recipe; geographic coverage is tracked separately for localisation.
    """
    if captured <= 0:
        return CoverageObservation()
    priced = sum(1 for p in products if p.regular_price is not None)
    qty = sum(1 for p in products if p.net_content_quantity is not None)
    unit = sum(1 for p in products if p.net_content_unit is not None)
    exhausted = captured < limit  # iteration ended before the cap -> we saw the whole path
    observed = "full" if (supports_full_catalog and exhausted) else "sample_only"
    obs = CoverageObservation(
        observed_catalog_scope=observed,
        price_coverage=_ratio(priced, captured),
        package_quantity_coverage=_ratio(qty, captured),
        package_unit_coverage=_ratio(unit, captured),
        geographic_scope_coverage=Decimal("1.0000") if supports_store_scope else Decimal("0.0000"),
    )
    obs.costing_eligibility = _costing_eligibility(obs)
    return obs


@dataclass(slots=True)
class ConfigStatus:
    configured: bool
    blocked_reason: str | None = None


def _parsebot_base_url(settings: Settings, retailer_slug: str) -> str:
    return getattr(settings, f"parse_bot_{retailer_slug}_base_url", "") or ""


def config_status(entry: MatrixEntry, settings: Settings) -> ConfigStatus:
    """Whether a provider is configured, and the blocking reason otherwise (no secrets)."""
    if entry.provider_code.startswith("parsebot-"):
        if not settings.parse_bot_api_key:
            return ConfigStatus(False, "blocked_by_missing_credentials")
        if not _parsebot_base_url(settings, entry.retailer_slug):
            return ConfigStatus(False, "blocked_by_missing_base_url")
        return ConfigStatus(True)
    if entry.provider_code == "apify-mercadona":
        if not settings.apify_api_token:
            return ConfigStatus(False, "blocked_by_missing_credentials")
        return ConfigStatus(True)
    return ConfigStatus(True)  # open-prices / demo need no credentials


@dataclass(slots=True)
class ProviderOnboardingReport:
    provider_code: str
    retailer_slug: str
    intended_role: str
    intended_catalog_scope: str
    configured: bool
    status: str = "not_started"
    captured: int | None = None
    schema_fingerprint: str | None = None
    mapper_status: str = "unknown"
    # Observed coverage from the real capture — distinct from the declared intent above.
    observed_catalog_scope: str = "unknown"
    costing_eligibility: str = "unknown"
    production_eligibility: bool = False
    rights: str = "under_review"
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider_code,
            "retailer": self.retailer_slug,
            "intended_role": self.intended_role,
            "intended_catalog_scope": self.intended_catalog_scope,
            "configured": self.configured,
            "status": self.status,
            "captured": self.captured,
            "schema_fingerprint": self.schema_fingerprint,
            "mapper_status": self.mapper_status,
            "observed_catalog_scope": self.observed_catalog_scope,
            "costing_eligibility": self.costing_eligibility,
            "production_eligibility": self.production_eligibility,
            "rights": self.rights,
            "error": self.error,
        }


@dataclass(slots=True)
class OnboardingMatrix:
    generated_at: str
    rows: list[ProviderOnboardingReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"generated_at": self.generated_at, "rows": [r.as_dict() for r in self.rows]}


def upsert_activation(
    db: Session,
    entry: MatrixEntry,
    *,
    now: datetime,
    transport_status: str = "unknown",
    mapper_status: str = "unknown",
    data_quality_status: str = "unknown",
    coverage: CoverageObservation | None = None,
) -> ProviderActivation:
    """Create/update the ProviderActivation row from the matrix (rights stay under review).

    Records the DECLARED intent (``intended_catalog_scope``) and, when a real capture was made,
    the OBSERVED coverage + costing eligibility. Production eligibility is never granted here —
    it requires the full production gate and a human approval.
    """
    row = db.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == entry.provider_code)
    ).scalar_one_or_none()
    if row is None:
        row = ProviderActivation(provider_code=entry.provider_code)
        db.add(row)
    row.intended_role = entry.intended_role
    row.intended_catalog_scope = entry.intended_catalog_scope
    row.activation_state = entry.activation_state
    row.expected_capabilities = list(entry.capabilities)
    row.transport_status = transport_status
    row.mapper_status = mapper_status
    row.data_quality_status = data_quality_status
    obs = coverage or CoverageObservation()
    row.observed_catalog_scope = obs.observed_catalog_scope
    row.price_coverage = obs.price_coverage
    row.package_quantity_coverage = obs.package_quantity_coverage
    row.package_unit_coverage = obs.package_unit_coverage
    row.geographic_scope_coverage = obs.geographic_scope_coverage
    row.costing_eligibility = obs.costing_eligibility
    # Onboarding NEVER grants production eligibility (full gate + human approval required).
    row.production_eligibility = False
    # Rights are NEVER auto-cleared here — always operator-reviewed (§O/§11).
    if row.data_rights_status in (None, "unknown"):
        row.data_rights_status = "under_review"
    row.development_only = entry.activation_state != "disabled"
    row.production_approved_at = None
    row.production_approved_by = None
    db.flush()
    _ = now  # reserved for future audit stamping
    return row


def get_entry(provider_code: str) -> MatrixEntry | None:
    return _MATRIX_BY_CODE.get(provider_code)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "RETAILER_MATRIX",
    "ConfigStatus",
    "CoverageObservation",
    "MatrixEntry",
    "OnboardingMatrix",
    "ProviderOnboardingReport",
    "config_status",
    "get_entry",
    "measure_coverage",
    "upsert_activation",
]
