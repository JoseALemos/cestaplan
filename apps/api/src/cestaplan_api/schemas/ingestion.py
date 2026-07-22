"""Pydantic request/response schemas for the ingestion admin API (spec §18, FASE B).

These models shape the JSON exchanged by :mod:`cestaplan_api.routers.ingestion_admin`.
Two invariants from the data model hold throughout:

- **Money and ratios are strings** (``coverage_ratio``, ``coverage_score``,
  ``expected_value`` …): a :class:`decimal.Decimal` is serialised with :func:`str`, never
  coerced to ``float``. Counts stay integers.
- **Nothing sensitive leaks**: no :class:`~cestaplan_api.models.ingestion.RawCapture` body,
  no response headers, no source secret or token is ever a field here. Entities are
  addressed by their public UUID (or a public connector ``code``), never their internal PK.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from cestaplan_api.ingestion import RunType

# --------------------------------------------------------------------------- #
# Connectors
# --------------------------------------------------------------------------- #


class ConnectorCapabilities(BaseModel):
    """Coarse capability summary sourced from the connector/adapter registry."""

    full_catalog: bool = False
    prices: bool = False
    promotions: bool = False
    availability: bool = False
    store_catalog: bool = False
    requires_network: bool = False


class DataSourceInfo(BaseModel):
    """Non-sensitive view of the connector's backing :class:`DataSource`."""

    slug: str
    name: str
    source_type: str
    legal_status: str
    is_enabled: bool
    url: str | None = None


class ConnectorSummary(BaseModel):
    """One connector as shown in the list view (keyed by its public ``code``)."""

    code: str
    name: str
    status: str | None = None
    legal_status: str
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    capabilities: ConnectorCapabilities | None = None


class ConnectorDetail(ConnectorSummary):
    """Full connector view: state, capabilities, legal footing, latest run and coverage."""

    last_attempt_at: datetime | None = None
    last_error: str | None = None
    data_source: DataSourceInfo | None = None
    latest_run: CrawlSummary | None = None
    coverage: CoverageRow | None = None


class ConnectorHealthReport(BaseModel):
    """The fused health report returned by the manual health-check endpoint."""

    code: str
    checked_at: datetime
    status: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    last_error: str | None = None
    last_run_status: str | None = None
    last_run_at: datetime | None = None
    coverage_ratio: str | None = None
    coverage_status: str | None = None
    fresh_prices: int | None = None


class ConnectorActionResponse(BaseModel):
    """Result of an enable/disable action on a connector."""

    code: str
    status: str
    changed: bool
    detail: str


# --------------------------------------------------------------------------- #
# Crawls
# --------------------------------------------------------------------------- #


class CrawlCounters(BaseModel):
    """Per-run outcome counters (integers, rolled up from jobs)."""

    discovered: int = 0
    fetched: int = 0
    parsed: int = 0
    accepted: int = 0
    rejected: int = 0
    quarantined: int = 0
    errors: int = 0


class CrawlCreateRequest(BaseModel):
    """Body of a manual crawl-run creation request."""

    model_config = ConfigDict(extra="forbid")

    retailer_code: str = Field(min_length=1, max_length=128)
    run_type: RunType
    store_id: uuid.UUID | None = None


class CrawlSummary(BaseModel):
    """A crawl run as shown in the list view."""

    id: str
    retailer_code: str
    store_id: str | None = None
    run_type: str
    status: str
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    coverage_score: str | None = None
    counters: CrawlCounters


class CrawlDetail(CrawlSummary):
    """A crawl run detail view (counters + coverage score + versions)."""

    connector_version: str | None = None
    parser_version: str | None = None
    jobs_total: int = 0
    requeued_jobs: int | None = None


# --------------------------------------------------------------------------- #
# Anomalies
# --------------------------------------------------------------------------- #


class AnomalySummary(BaseModel):
    """A detected price anomaly with its review lifecycle (no price data mutated)."""

    id: str
    anomaly_type: str
    severity: str
    status: str
    expected_value: str | None = None
    actual_value: str | None = None
    details: dict | None = None
    crawl_run_id: str | None = None
    price_observation_id: str | None = None
    created_at: datetime | None = None
    reviewed_at: datetime | None = None


class AnomalyReviewResponse(BaseModel):
    """Result of approving/rejecting an anomaly (clears quarantine only)."""

    anomaly: AnomalySummary
    detail: str


# --------------------------------------------------------------------------- #
# Coverage & sources
# --------------------------------------------------------------------------- #


class CoverageRow(BaseModel):
    """Latest coverage snapshot for a retailer (optionally a store), honest status."""

    retailer_code: str
    retailer_name: str
    store_id: str | None = None
    store_name: str | None = None
    observed_at: datetime | None = None
    status: str
    expected_products: int = 0
    discovered_products: int = 0
    priced_products: int = 0
    fresh_prices: int = 0
    stale_prices: int = 0
    estimated_prices: int = 0
    unavailable_products: int = 0
    coverage_ratio: str | None = None
    weighted_coverage_ratio: str | None = None


class SourceRow(BaseModel):
    """A data source with its legal footing and review paper trail."""

    slug: str
    name: str
    legal_status: str
    terms_reviewed_at: datetime | None = None
    robots_reviewed_at: datetime | None = None
    notes: str | None = None


# Resolve forward references between ConnectorDetail and CrawlSummary/CoverageRow.
ConnectorDetail.model_rebuild()


__all__ = [
    "AnomalyReviewResponse",
    "AnomalySummary",
    "ConnectorActionResponse",
    "ConnectorCapabilities",
    "ConnectorDetail",
    "ConnectorHealthReport",
    "ConnectorSummary",
    "CoverageRow",
    "CrawlCounters",
    "CrawlCreateRequest",
    "CrawlDetail",
    "CrawlSummary",
    "DataSourceInfo",
    "SourceRow",
]
