"""Retailer onboarding matrix (spec §1) — declares the seven chains + their roles/rights.

Pure data + small helpers. Declares each provider's intended role, catalogue scope, roll-out
stage, declared capabilities and rights, and computes an honest per-provider status from the
configuration actually present (credentials/base URLs) — a chain is never marked ready without
a real capture. Nothing here activates production or shows a secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import ProviderActivation


@dataclass(frozen=True, slots=True)
class MatrixEntry:
    provider_code: str
    retailer_slug: str
    intended_role: str
    catalog_scope: str  # full | partial | complementary
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
    catalog_scope: str
    configured: bool
    status: str = "not_started"
    captured: int | None = None
    schema_fingerprint: str | None = None
    mapper_status: str = "unknown"
    rights: str = "under_review"
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider_code,
            "retailer": self.retailer_slug,
            "intended_role": self.intended_role,
            "catalog_scope": self.catalog_scope,
            "configured": self.configured,
            "status": self.status,
            "captured": self.captured,
            "schema_fingerprint": self.schema_fingerprint,
            "mapper_status": self.mapper_status,
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
) -> ProviderActivation:
    """Create/update the ProviderActivation row from the matrix (rights stay under review)."""
    row = db.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == entry.provider_code)
    ).scalar_one_or_none()
    if row is None:
        row = ProviderActivation(provider_code=entry.provider_code)
        db.add(row)
    row.intended_role = entry.intended_role
    row.catalog_scope = entry.catalog_scope
    row.activation_state = entry.activation_state
    row.expected_capabilities = list(entry.capabilities)
    row.transport_status = transport_status
    row.mapper_status = mapper_status
    row.data_quality_status = data_quality_status
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
    "MatrixEntry",
    "OnboardingMatrix",
    "ProviderOnboardingReport",
    "config_status",
    "get_entry",
    "upsert_activation",
]
