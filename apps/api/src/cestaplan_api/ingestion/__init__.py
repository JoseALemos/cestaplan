"""Price-ingestion subsystem: connector contract, enums and value objects.

This package is the shared foundation for retailer connectors and the ingestion
pipeline. It has no ORM/database dependencies; the persistence models live in
:mod:`cestaplan_api.models.ingestion` and reuse the enums defined here.
"""

from __future__ import annotations

from cestaplan_api.ingestion.contracts import (
    AnomalyStatus,
    AnomalyType,
    Capabilities,
    ConnectorStatus,
    CoverageStatus,
    FetchResult,
    HealthResult,
    JobStatus,
    LegalStatus,
    NormalizedObservation,
    ParseResult,
    PriceScope,
    PriceType,
    PromotionInfo,
    PromotionType,
    RetailerConnector,
    RunStatus,
    RunType,
    Severity,
    SourcePolicy,
    SourceRef,
    StoreResolutionResult,
    ValidationResult,
    enum_values,
)

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
