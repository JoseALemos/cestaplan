"""Price-catalog provider contract (FASE 1).

A provider-agnostic layer over the existing ingestion subsystem: every external price
source (Parse.bot, Apify, Open Prices, demo) implements :class:`PriceCatalogProvider` and
emits the same :class:`ExternalCatalogProduct`. The persistence, history, validation,
anomaly, coverage, queue and worker machinery is reused from ``cestaplan_api.ingestion`` —
this module only adds the richer, catalog-oriented provider abstraction.

Design rules baked in here (see docs/PRICE_PROVIDERS.md):
- Money is always :class:`~decimal.Decimal`, never ``float``.
- ``canonical_name`` is internal and never requested from a provider.
- No provider is ever presented as an official retailer source.
- HTTP/transport lives in each provider's client; this contract is pure domain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

# Reuse the existing scope/type vocabularies (single source of truth).
from cestaplan_api.ingestion.contracts import PriceScope, PriceType


class SellUnit(StrEnum):
    PACKAGE = "package"
    UNIT = "unit"
    WEIGHT = "weight"
    VOLUME = "volume"


class ContentUnit(StrEnum):
    G = "g"
    KG = "kg"
    ML = "ml"
    L = "l"
    UNIT = "unit"


class Availability(StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    LIMITED = "limited"
    UNKNOWN = "unknown"


class ProviderVerificationStatus(StrEnum):
    """Verification state of a provider observation (distinct from mapping review state)."""

    PROVIDER_REPORTED = "provider_reported"
    AUTOMATICALLY_VALIDATED = "automatically_validated"
    QUARANTINED = "quarantined"
    MANUALLY_VERIFIED = "manually_verified"
    REJECTED = "rejected"


class ProviderKind(StrEnum):
    """How a source relates to the retailer — never 'official'."""

    INDEPENDENT = "independent"  # third-party scraper API (Parse.bot, Apify actor)
    COMMUNITY = "community"  # crowd-sourced (Open Prices)
    DEMO = "demo"  # synthetic fixtures


class ProviderStatus(StrEnum):
    ACTIVE_WHEN_CONFIGURED = "active_when_configured"
    EXPERIMENTAL = "experimental"
    COMPLEMENTARY = "complementary"
    NOT_CONFIGURED = "not_configured"
    UNSUPPORTED = "unsupported"
    PARTIAL_SOURCE_REQUIRED = "partial_source_required"
    AUTHORIZED_FEED_REQUIRED = "authorized_feed_required"


@dataclass(frozen=True, slots=True)
class ProviderPromotion:
    """Structured promotion, kept separate from the regular price so they never mix."""

    price_type: PriceType
    raw_text: str | None = None
    promotional_price: Decimal | None = None
    loyalty_price: Decimal | None = None
    required_quantity: int | None = None
    charged_quantity: int | None = None
    percentage_discount: Decimal | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    conditions: dict[str, object] | None = None


@dataclass(slots=True)
class ExternalCatalogProduct:
    """The single normalized contract every provider produces (spec §6).

    Only ``provider``, ``retailer_slug``, ``external_product_id``, ``product_name``,
    ``sell_unit``, ``regular_price``, ``currency`` and ``observed_at`` are mandatory; the
    rest are optional and left ``None`` when the source does not supply them (never invented).
    """

    provider: str
    retailer_slug: str
    external_product_id: str
    product_name: str
    sell_unit: SellUnit
    regular_price: Decimal
    currency: str
    price_scope: PriceScope
    observed_at: datetime
    availability: Availability = Availability.UNKNOWN
    variable_weight: bool = False
    verification_status: ProviderVerificationStatus = (
        ProviderVerificationStatus.PROVIDER_REPORTED
    )
    confidence_score: Decimal = Decimal("1.0")
    # optional descriptors
    external_variant_id: str | None = None
    external_store_id: str | None = None
    postal_code: str | None = None
    brand: str | None = None
    category: str | None = None
    barcode: str | None = None
    image_url: str | None = None
    product_url: str | None = None
    package_quantity: Decimal | None = None
    package_unit: ContentUnit | None = None
    net_content_quantity: Decimal | None = None
    net_content_unit: ContentUnit | None = None
    promotional_price: Decimal | None = None
    loyalty_price: Decimal | None = None
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    promotion: ProviderPromotion | None = None
    expires_at: datetime | None = None
    raw_source_reference: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can do — drives the scheduler and admin UI."""

    full_catalog: bool = False
    store_scope: bool = False
    incremental_sync: bool = False
    promotions: bool = False
    categories: bool = False
    search: bool = False


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Non-secret description of a source, for the admin API and audit trail."""

    provider_code: str
    retailer_slug: str
    kind: ProviderKind
    status: ProviderStatus
    official: bool = False  # always False here — no source is a retailer's official API
    catalog_type: str = "unknown"
    attribution: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class HealthStatus:
    ok: bool
    detail: str
    checked_at: datetime | None = None
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class StoreRef:
    external_store_id: str
    name: str | None = None
    postal_code: str | None = None
    province: str | None = None
    price_scope: PriceScope = PriceScope.UNKNOWN


@dataclass(frozen=True, slots=True)
class CategoryRef:
    external_id: str
    name: str
    parent_external_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProductQuery:
    """Bounded query for a sync: never 'download all of Spain' by default."""

    store_external_id: str | None = None
    postal_code: str | None = None
    category_external_id: str | None = None
    search: str | None = None
    barcodes: tuple[str, ...] = ()
    max_products: int | None = None


class PriceCatalogProvider(ABC):
    """Common interface for every price-catalog source (spec §3).

    Implementations own their transport (HTTP client) but return only normalized domain
    objects. Unsupported operations return an empty iterator / raise ``NotSupportedError``
    from :mod:`cestaplan_api.ingestion.providers.exceptions`, never fabricate data.
    """

    #: Stable machine code, e.g. ``"parsebot-dia"``.
    provider_code: str = "base"

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def get_source_metadata(self) -> ProviderMetadata: ...

    @abstractmethod
    def health_check(self) -> HealthStatus: ...

    def list_stores(self) -> list[StoreRef]:
        return []

    def list_categories(self) -> list[CategoryRef]:
        return []

    @abstractmethod
    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]: ...

    def get_product(self, external_product_id: str) -> ExternalCatalogProduct | None:
        return None

    def iterate_promotions(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        return iter(())

    # -- capability shorthands (default to declared capabilities) -------------- #
    def supports_full_catalog(self) -> bool:
        return self.capabilities().full_catalog

    def supports_store_scope(self) -> bool:
        return self.capabilities().store_scope

    def supports_incremental_sync(self) -> bool:
        return self.capabilities().incremental_sync


# ``normalize_product`` / ``normalize_price`` from the spec live in each provider's
# ``mapping.py`` (payload -> ExternalCatalogProduct); the shared, provider-independent
# normalization/validation helpers live in ``normalization.py`` / ``validation.py``.

__all__ = [
    "Availability",
    "CategoryRef",
    "ContentUnit",
    "ExternalCatalogProduct",
    "HealthStatus",
    "PriceCatalogProvider",
    "ProductQuery",
    "ProviderCapabilities",
    "ProviderKind",
    "ProviderMetadata",
    "ProviderPromotion",
    "ProviderStatus",
    "ProviderVerificationStatus",
    "SellUnit",
    "StoreRef",
]
