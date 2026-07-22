"""Connector contract, enums and value objects for the price-ingestion subsystem.

This module is the shared foundation every retailer connector and every ingestion
pipeline stage builds on. It deliberately imports nothing from the ORM layer so it can
be reused by workers, adapters and tests without pulling in SQLAlchemy or a database.

Design invariants:
- Money and physical quantities are :class:`decimal.Decimal`, never ``float``.
- Enums are :class:`enum.StrEnum` so their ``.value`` doubles as the value stored in the
  VARCHAR + CHECK columns of :mod:`cestaplan_api.models.ingestion`.
- Every optional connector capability degrades gracefully: default connector methods
  return a *controlled* "not supported" result object and never raise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum

# --------------------------------------------------------------------------- #
# Enums (string enums — the ``.value`` is what the DB stores)
# --------------------------------------------------------------------------- #


class LegalStatus(StrEnum):
    """Legal footing under which a data source may be ingested."""

    UNKNOWN = "unknown"
    PUBLIC = "public"
    AUTHORIZED = "authorized"
    PERMISSION_REQUIRED = "permission_required"
    PROHIBITED = "prohibited"


class ConnectorStatus(StrEnum):
    """Operational health of a connector for a retailer/store."""

    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    PERMISSION_REQUIRED = "permission_required"
    TEMPORARILY_BLOCKED = "temporarily_blocked"
    PARSER_BROKEN = "parser_broken"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PARTIAL_ONLY = "partial_only"


class RunType(StrEnum):
    """The kind of work a crawl run performs."""

    DISCOVERY = "discovery"
    CATALOG = "catalog"
    PRICES = "prices"
    OFFERS = "offers"
    HEALTH = "health"


class RunStatus(StrEnum):
    """Lifecycle of a :class:`~cestaplan_api.models.ingestion.CrawlRun`."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    """Lifecycle of a :class:`~cestaplan_api.models.ingestion.CrawlJob` in the
    SELECT ... FOR UPDATE SKIP LOCKED queue."""

    QUEUED = "queued"
    LOCKED = "locked"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class PriceScope(StrEnum):
    """Geographic/administrative scope a price applies to (most to least specific)."""

    EXACT_STORE = "exact_store"
    DELIVERY_ZONE = "delivery_zone"
    POSTAL_CODE = "postal_code"
    MUNICIPALITY = "municipality"
    PROVINCE = "province"
    REGION = "region"
    NATIONAL = "national"
    UNKNOWN = "unknown"


class PriceType(StrEnum):
    """The nature of an observed price."""

    REGULAR = "regular"
    PROMOTIONAL = "promotional"
    LOYALTY = "loyalty"
    MANUAL = "manual"
    RECEIPT = "receipt"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class PromotionType(StrEnum):
    """Shape of a promotion rule attached to a price observation."""

    PERCENTAGE = "percentage"
    FIXED = "fixed"
    NXM = "nxm"
    SECOND_UNIT = "second_unit"
    MIN_QUANTITY = "min_quantity"
    PACK = "pack"


class AnomalyType(StrEnum):
    """Reference vocabulary for price-anomaly classification.

    The ``anomaly_type`` column is a free-text string so detectors can introduce new
    kinds without a migration; these are the canonical, well-known values.
    """

    PRICE_SPIKE = "price_spike"
    PRICE_DROP = "price_drop"
    ZERO_OR_NEGATIVE = "zero_or_negative"
    UNIT_MISMATCH = "unit_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DUPLICATE = "duplicate"
    STALE = "stale"
    OUTLIER = "outlier"
    MISSING_FIELD = "missing_field"
    BLOCK_PAGE = "block_page"


class Severity(StrEnum):
    """Severity of a detected anomaly."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AnomalyStatus(StrEnum):
    """Review lifecycle of a price anomaly."""

    OPEN = "open"
    QUARANTINED = "quarantined"
    APPROVED = "approved"
    REJECTED = "rejected"


class CoverageStatus(StrEnum):
    """Coverage grade of a retailer/store snapshot."""

    COMPLETE = "complete"
    HIGH = "high"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    STALE = "stale"
    NONE = "none"


def enum_values(enum_cls: type[Enum]) -> tuple[str, ...]:
    """Return the ordered ``.value`` tuple of a string enum.

    Used by the ORM layer to feed ``enum_col(*values, name=...)`` so the database CHECK
    constraint stays in lockstep with the enum defined here (single source of truth).
    """
    return tuple(str(member.value) for member in enum_cls)


# --------------------------------------------------------------------------- #
# Capabilities & policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Declares what a connector can extract from its retailer.

    Every flag defaults to ``False``; a connector opts in only to what it truly supports.
    Consumers use these flags to decide scheduling, coverage expectations and which
    pipeline stages to run.
    """

    full_catalog: bool = False
    partial_catalog: bool = False
    prices: bool = False
    promotions: bool = False
    loyalty_prices: bool = False
    availability: bool = False
    exact_store_scope: bool = False
    delivery_zone_scope: bool = False
    regional_scope: bool = False
    national_scope: bool = False
    product_images: bool = False
    barcodes: bool = False
    nutrition: bool = False
    incremental_sync: bool = False


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Access policy a connector must honour when talking to its source.

    ``request_delay`` is seconds between requests; ``max_concurrency`` the number of
    in-flight requests allowed. ``legal_status`` records the legal footing and ``contact``
    an optional operator/abuse contact.
    """

    allowed_domains: tuple[str, ...] = ()
    request_delay: float = 1.0
    max_concurrency: int = 1
    respects_robots: bool = True
    legal_status: LegalStatus = LegalStatus.UNKNOWN
    contact: str | None = None


# --------------------------------------------------------------------------- #
# Value objects (immutable results passed between pipeline stages)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Traceability pointer for where an observation came from."""

    source_slug: str | None = None
    source_url: str | None = None
    connector_version: str | None = None
    parser_version: str | None = None
    raw_capture_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionInfo:
    """Structured promotion metadata carried on a normalized observation."""

    promotion_type: PromotionType
    raw_text: str | None = None
    required_quantity: int | None = None
    charged_quantity: int | None = None
    percentage_discount: Decimal | None = None
    fixed_discount: Decimal | None = None
    loyalty_required: bool = False
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    conditions: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """A single, source-agnostic price observation ready for validation/persistence.

    ``variant_ref`` is the connector-local identifier of the product variant the price
    is for (typically ``ExternalProduct.external_id`` or a variant key); the persistence
    layer resolves it to a :class:`~cestaplan_api.models.ingestion.ProductVariant`.
    """

    variant_ref: str
    amount: Decimal
    currency: str
    price_scope: PriceScope
    price_type: PriceType
    observed_at: datetime
    unit_amount: Decimal | None = None
    unit_code: str | None = None
    promotion: PromotionInfo | None = None
    requires_loyalty: bool = False
    available: bool | None = None
    confidence: Decimal = Decimal("1.0")
    source: SourceRef | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a network/source fetch. ``payload`` holds decoded records for
    discovery/catalog fetches; ``content`` the raw bytes for a single-resource fetch."""

    ok: bool = False
    supported: bool = True
    url: str | None = None
    status_code: int | None = None
    content: bytes | None = None
    content_type: str | None = None
    body_hash: str | None = None
    payload: object | None = None
    is_block_page: bool = False
    error: str | None = None
    raw_capture_ref: str | None = None

    @classmethod
    def unsupported(cls, reason: str = "operation not supported by this connector") -> FetchResult:
        return cls(ok=False, supported=False, error=reason)


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Outcome of parsing/normalizing a captured response into observations."""

    ok: bool = False
    supported: bool = True
    observations: tuple[NormalizedObservation, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def unsupported(cls, reason: str = "operation not supported by this connector") -> ParseResult:
        return cls(ok=False, supported=False, error=reason)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating a normalized observation before persistence."""

    valid: bool = False
    supported: bool = True
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    anomaly_type: AnomalyType | None = None
    severity: Severity | None = None

    @classmethod
    def unsupported(
        cls, reason: str = "operation not supported by this connector"
    ) -> ValidationResult:
        return cls(valid=False, supported=False, errors=(reason,))


@dataclass(frozen=True, slots=True)
class HealthResult:
    """Outcome of a connector health probe."""

    status: ConnectorStatus = ConnectorStatus.UNSUPPORTED
    ok: bool = False
    supported: bool = True
    checked_at: datetime | None = None
    latency_ms: int | None = None
    detail: str | None = None

    @classmethod
    def unsupported(
        cls, reason: str = "health check not supported by this connector"
    ) -> HealthResult:
        return cls(
            status=ConnectorStatus.UNSUPPORTED, ok=False, supported=False, detail=reason
        )


@dataclass(frozen=True, slots=True)
class StoreResolutionResult:
    """Outcome of resolving a request (postal code / store id) to a concrete store scope."""

    ok: bool = False
    supported: bool = True
    resolved_retailer_code: str | None = None
    resolved_store_ref: str | None = None
    external_store_id: str | None = None
    delivery_zone_id: str | None = None
    scope: PriceScope = PriceScope.UNKNOWN
    resolution_method: str | None = None
    confidence: Decimal = Decimal("0")
    evidence: dict[str, object] | None = None
    error: str | None = None

    @classmethod
    def unsupported(
        cls, reason: str = "store resolution not supported by this connector"
    ) -> StoreResolutionResult:
        return cls(ok=False, supported=False, error=reason)


# --------------------------------------------------------------------------- #
# Connector contract
# --------------------------------------------------------------------------- #


class RetailerConnector(ABC):
    """Abstract contract every retailer connector implements.

    Only :meth:`capabilities` and :meth:`source_policy` (plus the identity properties)
    are abstract. Every data-access method has a default implementation that returns a
    controlled "not supported" result and never raises, so a minimal connector can
    declare a narrow capability set and inherit safe no-ops for the rest.
    """

    #: Stable short code identifying the retailer (e.g. ``"mercadona"``).
    retailer_code: str = ""
    #: Version of the connector's fetch/orchestration logic.
    connector_version: str = "0.0.0"
    #: Version of the connector's parsing logic (bumped when parse output changes).
    parser_version: str = "0.0.0"

    # -- required -------------------------------------------------------- #
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Return the connector's declared capabilities."""

    @abstractmethod
    def source_policy(self) -> SourcePolicy:
        """Return the access policy the connector honours."""

    # -- health ---------------------------------------------------------- #
    def health_check(self) -> HealthResult:
        """Probe source reachability. Default: unsupported."""
        return HealthResult.unsupported()

    # -- store resolution & discovery ------------------------------------ #
    def resolve_store(
        self,
        *,
        postal_code: str | None = None,
        store_id: str | None = None,
        external_store_id: str | None = None,
    ) -> StoreResolutionResult:
        """Resolve a request to a concrete store/zone scope. Default: unsupported."""
        return StoreResolutionResult.unsupported()

    def discover_stores(self) -> FetchResult:
        """Enumerate the retailer's stores. Default: unsupported."""
        return FetchResult.unsupported()

    def discover_products(self, *, cursor: str | None = None) -> FetchResult:
        """Enumerate product references (paginated via ``cursor``). Default: unsupported."""
        return FetchResult.unsupported()

    # -- fetch ----------------------------------------------------------- #
    def fetch_product(self, external_id: str, **kwargs: object) -> FetchResult:
        """Fetch a single product resource. Default: unsupported."""
        return FetchResult.unsupported()

    def fetch_category(self, category: str, **kwargs: object) -> FetchResult:
        """Fetch a category listing. Default: unsupported."""
        return FetchResult.unsupported()

    def fetch_offers(self, **kwargs: object) -> FetchResult:
        """Fetch current offers/promotions. Default: unsupported."""
        return FetchResult.unsupported()

    # -- parse & normalize ----------------------------------------------- #
    def parse_product(self, capture: object, **kwargs: object) -> ParseResult:
        """Parse a raw capture into structured records. Default: unsupported."""
        return ParseResult.unsupported()

    def normalize_product(self, parsed: object, **kwargs: object) -> ParseResult:
        """Normalize parsed records into :class:`NormalizedObservation`s. Default: unsupported."""
        return ParseResult.unsupported()

    def validate_observation(self, observation: NormalizedObservation) -> ValidationResult:
        """Connector-specific validation of a normalized observation. Default: unsupported."""
        return ValidationResult.unsupported()

    # -- pagination & sync capabilities ---------------------------------- #
    def get_next_cursor(self, current: object) -> str | None:
        """Return the next pagination cursor, or ``None`` when exhausted. Default: ``None``."""
        return None

    def supports_incremental_sync(self) -> bool:
        """Whether the connector can fetch only what changed. Default: ``False``."""
        return False

    def supports_conditional_requests(self) -> bool:
        """Whether the connector honours ETag/If-Modified-Since. Default: ``False``."""
        return False


__all__ = [
    "AnomalyStatus",
    "AnomalyType",
    "Capabilities",
    "ConnectorStatus",
    "CoverageStatus",
    "FetchResult",
    "HealthResult",
    "JobStatus",
    "LegalStatus",
    "NormalizedObservation",
    "ParseResult",
    "PriceScope",
    "PriceType",
    "PromotionInfo",
    "PromotionType",
    "RetailerConnector",
    "RunStatus",
    "RunType",
    "Severity",
    "SourcePolicy",
    "SourceRef",
    "StoreResolutionResult",
    "ValidationResult",
    "enum_values",
]
