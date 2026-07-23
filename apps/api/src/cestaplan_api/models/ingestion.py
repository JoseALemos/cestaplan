"""ORM models for the price-ingestion subsystem (FASE A foundation).

These tables model the crawl/parse/normalize/validate pipeline and its append-only price
history. They reuse the existing commercial catalogue (:class:`Retailer`, :class:`Store`,
:class:`Product`, :class:`DataSource`) via foreign keys.

Conventions (see :mod:`cestaplan_api.models.base` and ``docs/DATA_MODEL.md``):
- Every table carries the internal ``bigint`` identity PK plus the public ``uuid``.
- Money/quantities are ``Numeric``/``Decimal``, never ``float``.
- Enums are VARCHAR + CHECK (``enum_col``); their allowed values come from the string
  enums in :mod:`cestaplan_api.ingestion.contracts` (single source of truth).
- :class:`PriceObservation` is append-only history: build history by inserting rows and
  closing the previous row's ``valid_until``; never destructively UPDATE a price.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.ingestion.contracts import (
    AnomalyStatus,
    ConnectorStatus,
    CoverageStatus,
    JobStatus,
    PriceScope,
    PriceType,
    PromotionType,
    RunStatus,
    RunType,
    Severity,
    enum_values,
)
from cestaplan_api.models.base import BaseModel, enum_col, money
from cestaplan_api.models.catalog import VERIFICATION_STATUS

# Enum value tuples derived from the contract enums (kept in lockstep via enum_values).
CONNECTOR_STATUS = enum_values(ConnectorStatus)
RUN_TYPE = enum_values(RunType)
RUN_STATUS = enum_values(RunStatus)
JOB_STATUS = enum_values(JobStatus)
PRICE_SCOPE = enum_values(PriceScope)
PRICE_TYPE = enum_values(PriceType)
PROMOTION_TYPE = enum_values(PromotionType)
ANOMALY_SEVERITY = enum_values(Severity)
ANOMALY_STATUS = enum_values(AnomalyStatus)
COVERAGE_STATUS = enum_values(CoverageStatus)


class ConnectorState(BaseModel):
    """Operational state of a connector for a retailer (optionally a specific store).

    Tracks the circuit-breaker signals (consecutive failures, open-until) and the
    connector/parser versions in effect. Unique per (retailer, store, connector_version).
    """

    __tablename__ = "connector_state"
    __table_args__ = (
        Index(
            "ux_connector_state_scope",
            "retailer_id",
            "store_id",
            "connector_version",
            unique=True,
        ),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    connector_version: Mapped[str] = mapped_column(Text, nullable=False)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_col(*CONNECTOR_STATUS, name="connector_state_status"),
        nullable=False,
        server_default=ConnectorStatus.ACTIVE.value,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CrawlRun(BaseModel):
    """A single scheduled crawl execution over a retailer/store for a run type.

    Counters roll up per-job outcomes; ``coverage_score`` is an optional 0..1 estimate of
    how much of the expected catalogue this run covered.
    """

    __tablename__ = "crawl_run"
    __table_args__ = (
        Index("ix_crawl_run_retailer_status", "retailer_id", "status"),
        Index("ix_crawl_run_scheduled", "scheduled_at"),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    run_type: Mapped[str] = mapped_column(
        enum_col(*RUN_TYPE, name="crawl_run_type"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_col(*RUN_STATUS, name="crawl_run_status"),
        nullable=False,
        server_default=RunStatus.QUEUED.value,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    fetched_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    parsed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    accepted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    rejected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    quarantined_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    coverage_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    connector_version: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)


class CrawlJob(BaseModel):
    """A unit of work inside a crawl run, dispatched via SELECT ... FOR UPDATE SKIP LOCKED.

    The ``(status, available_at, priority)`` index backs the queue take path. ``attempts``
    vs ``max_attempts`` drive the retry/dead-letter policy.
    """

    __tablename__ = "crawl_job"
    __table_args__ = (
        # Queue take path: filter by status, order by available_at then priority.
        Index("ix_crawl_job_queue", "status", "available_at", "priority"),
        Index("ix_crawl_job_run", "crawl_run_id"),
    )

    crawl_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("crawl_run.id"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        enum_col(*JOB_STATUS, name="crawl_job_status"),
        nullable=False,
        server_default=JobStatus.QUEUED.value,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawCapture(BaseModel):
    """An immutable capture of a source response for reproducibility and re-parsing.

    ``response_headers`` must have secrets redacted before storage. ``body_data`` holds the
    (possibly compressed) body when retention allows; ``body_hash`` (sha256) enables
    dedup and change detection. ``is_block_page`` flags anti-bot/interstitial responses.
    """

    __tablename__ = "raw_capture"
    __table_args__ = (
        Index("ix_raw_capture_body_hash", "body_hash"),
        Index("ix_raw_capture_retailer_captured", "retailer_id", "captured_at"),
    )

    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("crawl_run.id")
    )
    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    request_method: Mapped[str] = mapped_column(Text, nullable=False, server_default="GET")
    response_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    content_encoding: Mapped[str | None] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_headers: Mapped[dict | None] = mapped_column(JSONB)
    body_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_block_page: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    retention_policy: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)


class ExternalProduct(BaseModel):
    """A product as identified in a retailer's own namespace (its ``external_id``).

    Optionally linked to a canonical :class:`Product`. Unique per (retailer, external_id);
    ``active`` tracks whether the retailer still lists it.
    """

    __tablename__ = "external_product"
    __table_args__ = (
        Index(
            "ux_external_product_retailer_external",
            "retailer_id",
            "external_id",
            unique=True,
        ),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    external_url: Mapped[str | None] = mapped_column(Text)
    canonical_product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product.id")
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class ProductVariant(BaseModel):
    """A specific sellable variant of a product at a retailer (a given package/size).

    A price observation is made against a variant, not the abstract product, so different
    package sizes keep independent price histories.
    """

    __tablename__ = "product_variant"
    __table_args__ = (
        Index("ix_product_variant_retailer", "retailer_id"),
        Index("ix_product_variant_external_product", "external_product_id"),
    )

    product_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("product.id"))
    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    external_product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("external_product.id"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    package_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    package_unit: Mapped[str | None] = mapped_column(Text)
    package_count: Mapped[int | None] = mapped_column(Integer)
    # How the variant is sold: as a fixed package, a counted unit, or by weight/volume.
    # One of SELL_UNITS; drives how the planner turns a recipe quantity into a cost.
    sell_unit: Mapped[str | None] = mapped_column(Text)
    # Net content of the variant regardless of how it is sold (e.g. a 400 g can sold as
    # one "unit" still has net_content_quantity=400, net_content_unit="g"). This is what
    # lets the engine cost a recipe measured in g/ml against a counted product.
    net_content_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    net_content_unit: Mapped[str | None] = mapped_column(Text)
    # True when the item is sold by variable weight (e.g. fresh fish billed at the till);
    # its price is a unit_price and the package_quantity is nominal.
    variable_weight: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Reference price per unit_price_unit (e.g. 2.30 €/kg), when the supplier provides it.
    unit_price: Mapped[Decimal | None] = mapped_column(money())
    unit_price_unit: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class PriceObservation(BaseModel):
    """Append-only price history for a product variant at a scope and instant.

    History is built by inserting rows and closing the prior row's ``valid_until``; rows
    are never destructively updated. ``valid_from``/``valid_until`` model the effective
    interval, ``observed_at`` the moment of observation, ``expires_at`` an optional TTL.
    """

    __tablename__ = "price_observation"
    __table_args__ = (
        Index("ix_price_obs_variant_valid_from", "product_variant_id", "valid_from"),
        Index(
            "ix_price_obs_retailer_store_observed",
            "retailer_id",
            "store_id",
            "observed_at",
        ),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    # No delivery_zone table yet; kept as a nullable bigint reference.
    delivery_zone_id: Mapped[int | None] = mapped_column(BigInteger)
    product_variant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product_variant.id"), nullable=False
    )
    price_scope: Mapped[str] = mapped_column(
        enum_col(*PRICE_SCOPE, name="price_observation_scope"), nullable=False
    )
    price_type: Mapped[str] = mapped_column(
        enum_col(*PRICE_TYPE, name="price_observation_type"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(money(), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    unit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    unit_code: Mapped[str | None] = mapped_column(Text)
    promotion_text: Mapped[str | None] = mapped_column(Text)
    requires_loyalty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    promotion_valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promotion_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available: Mapped[bool | None] = mapped_column(Boolean)
    source_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("data_source.id"))
    source_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    raw_capture_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("raw_capture.id")
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("crawl_run.id")
    )
    connector_version: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        enum_col(*VERIFICATION_STATUS, name="price_observation_verification_status"),
        nullable=False,
        server_default="unverified",
    )


class PromotionRule(BaseModel):
    """Structured promotion attached to a price observation (parsed from ``promotion_text``)."""

    __tablename__ = "promotion_rule"
    __table_args__ = (Index("ix_promotion_rule_observation", "price_observation_id"),)

    price_observation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("price_observation.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        enum_col(*PROMOTION_TYPE, name="promotion_rule_type"), nullable=False
    )
    required_quantity: Mapped[int | None] = mapped_column(Integer)
    charged_quantity: Mapped[int | None] = mapped_column(Integer)
    percentage_discount: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    fixed_discount: Mapped[Decimal | None] = mapped_column(money())
    loyalty_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conditions: Mapped[dict | None] = mapped_column(JSONB)
    raw_text: Mapped[str | None] = mapped_column(Text)


class PriceAnomaly(BaseModel):
    """A detected anomaly in an observation or crawl run, with a review lifecycle.

    ``anomaly_type`` is free text (detectors add kinds without a migration); the canonical
    vocabulary lives in :class:`cestaplan_api.ingestion.contracts.AnomalyType`.
    """

    __tablename__ = "price_anomaly"
    __table_args__ = (
        Index("ix_price_anomaly_status", "status"),
        Index("ix_price_anomaly_observation", "price_observation_id"),
    )

    price_observation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("price_observation.id")
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("crawl_run.id")
    )
    anomaly_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        enum_col(*ANOMALY_SEVERITY, name="price_anomaly_severity"), nullable=False
    )
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    details: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        enum_col(*ANOMALY_STATUS, name="price_anomaly_status"),
        nullable=False,
        server_default=AnomalyStatus.OPEN.value,
    )
    # ``created_at`` (when the anomaly was recorded) comes from TimestampMixin.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StoreResolution(BaseModel):
    """A record of resolving a request (postal code / store id) to a concrete store scope."""

    __tablename__ = "store_resolution"
    __table_args__ = (
        Index("ix_store_resolution_postal", "requested_postal_code"),
        Index("ix_store_resolution_resolved_store", "resolved_store_id"),
    )

    requested_postal_code: Mapped[str | None] = mapped_column(Text)
    requested_store_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    resolved_store_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("store.id")
    )
    external_store_id: Mapped[str | None] = mapped_column(Text)
    delivery_zone_id: Mapped[int | None] = mapped_column(BigInteger)
    scope: Mapped[str] = mapped_column(
        enum_col(*PRICE_SCOPE, name="store_resolution_scope"), nullable=False
    )
    resolution_method: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class CoverageSnapshot(BaseModel):
    """A point-in-time measurement of price coverage for a retailer/store."""

    __tablename__ = "coverage_snapshot"
    __table_args__ = (
        Index("ix_coverage_snapshot_retailer_observed", "retailer_id", "observed_at"),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_products: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    discovered_products: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    priced_products: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    fresh_prices: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    stale_prices: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    estimated_prices: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    unavailable_products: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    weighted_coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(
        enum_col(*COVERAGE_STATUS, name="coverage_snapshot_status"), nullable=False
    )


# How a variant is sold/measured. Kept as documented string sets (validated on import)
# rather than DB enums, so a new supplier vocabulary never needs a migration.
SELL_UNITS = ("package", "unit", "weight", "volume")
# How an ingredient<->product mapping was produced, for audit and review triage.
# Documented vocabulary (match_method is free Text, no DB enum) — superset covering the
# provider-integration mapping methods (barcode_exact / normalized_name / alias_rule /
# semantic_candidate / llm_suggested) plus the earlier licensed-import ones.
MATCH_METHODS = (
    "barcode_exact",
    "manual",
    "normalized_name",
    "alias_rule",
    "semantic_candidate",
    "llm_suggested",
    "exact",
    "barcode",
    "token",
    "supplier_declared",
    "ai",
)


class SupplierFieldMapping(BaseModel):
    """Provider-agnostic map from a supplier's payload to our product/price contract.

    A licensed feed/catalogue arrives in the supplier's own shape. This row declares, per
    source, which supplier field feeds each of our canonical product/price fields (dotted
    paths allowed) plus optional unit aliases, so the importers stay provider-agnostic and
    never hardcode a supplier schema. It deliberately does NOT map ``canonical_name``: the
    recipe-ingredient link is resolved later by the mapping/review pipeline.
    """

    __tablename__ = "supplier_field_mapping"
    __table_args__ = (
        Index("ux_supplier_field_mapping_source_name", "source_name", unique=True),
    )

    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    data_source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_source.id")
    )
    # our canonical field -> supplier field (dotted path allowed), e.g.
    # {"product_name": "title", "amount": "price.value", "net_content_unit": "unit"}.
    field_map: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # supplier unit string -> our unit code, e.g. {"gramo": "g", "litro": "l"}.
    unit_aliases: Mapped[dict | None] = mapped_column(JSONB)
    default_currency: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text)


class ProviderUsage(BaseModel):
    """Cost/quota accounting for an external price provider operation (spec §11).

    One row per provider call/run so daily-run, daily-cost and per-run quotas can be
    enforced and surfaced in the admin API. Cost is Decimal money; never float.
    """

    __tablename__ = "provider_usage"
    __table_args__ = (
        Index("ix_provider_usage_provider_started", "provider", "started_at"),
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    product_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    estimated_cost: Mapped[Decimal | None] = mapped_column(money())
    currency: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawl_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("crawl_run.id")
    )


# Provider activation vocabularies (documented Text sets; validated in the activation gate).
DATA_RIGHTS_STATUS = (
    "unknown",
    "under_review",
    "development_only",
    "commercial_use_allowed",
    "display_allowed",
    "storage_allowed",
    "redistribution_forbidden",
    "rejected",
)
TRANSPORT_STATUS = ("unknown", "operational", "degraded", "down")
MAPPER_STATUS = ("unknown", "pending", "blocked", "verified")
DATA_QUALITY_STATUS = ("unknown", "accepted", "degraded", "insufficient", "quarantined")


class ProviderActivation(BaseModel):
    """Production-activation gate for a price provider (spec §O).

    Independent of whether the API merely works: a provider reaches production only when
    transport is operational, its mapper is verified, data quality is accepted, its data
    rights are compatible with the intended use, and a human has approved it
    (``production_approved_at``/``by``). ``development_only`` allows dev use without approval.
    """

    __tablename__ = "provider_activation"
    __table_args__ = (
        Index("ux_provider_activation_code", "provider_code", unique=True),
    )

    provider_code: Mapped[str] = mapped_column(Text, nullable=False)
    transport_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown"
    )
    mapper_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    data_quality_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown"
    )
    data_rights_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unknown"
    )
    development_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    production_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    production_approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
