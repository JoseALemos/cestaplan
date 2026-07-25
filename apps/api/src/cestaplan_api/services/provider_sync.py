"""Provider sync with dry-run / staging / production modes (spec §P).

Turns a provider's :class:`ExternalCatalogProduct` stream into append-only price observations,
guarded end to end:

- dry-run  : fetch + quality/coverage only; writes NOTHING, changes no current price.
- staging  : persists observations marked ``staging_only`` (never used by production plans).
- production: requires the activation gate (§O); a quarantined run never replaces good data.

Every persisted observation is tagged with its ``crawl_run_id`` and closes the prior open row
recording ``closed_by_run_id`` — so a run is logically reversible (§T) without any DELETE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceType
from cestaplan_api.ingestion.providers.activation import guard_production_sync
from cestaplan_api.ingestion.providers.contracts import (
    ExternalCatalogProduct,
    PriceCatalogProvider,
    ProductQuery,
)
from cestaplan_api.ingestion.providers.quality import evaluate_quality
from cestaplan_api.models import (
    CrawlRun,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
)
from cestaplan_api.services.observation_persistence import (
    OccurrenceProvenance,
    RecordMetrics,
    record_price_fact,
)


class SyncMode(StrEnum):
    DRY_RUN = "dry_run"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass(slots=True)
class SyncReport:
    provider: str
    mode: str
    run_id: str | None = None
    fetched: int = 0
    persisted_observations: int = 0
    # Two-layer metrics (spec §3/§9): new price facts vs reused facts, and provenance occurrences.
    observations_created: int = 0
    observations_reused: int = 0
    occurrences_created: int = 0
    occurrences_reused: int = 0
    quality_status: str = "insufficient"
    quarantined: bool = False
    reasons: list[str] = field(default_factory=list)
    coverage: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "run_id": self.run_id,
            "fetched": self.fetched,
            "persisted_observations": self.persisted_observations,
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "occurrences_created": self.occurrences_created,
            "occurrences_reused": self.occurrences_reused,
            "quality_status": self.quality_status,
            "quarantined": self.quarantined,
            "reasons": list(self.reasons),
            "coverage": self.coverage,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def run_provider_sync(
    db: Session,
    provider: PriceCatalogProvider,
    retailer: Retailer,
    settings: Settings,
    *,
    mode: SyncMode,
    query: ProductQuery | None = None,
    previous_count: int | None = None,
    as_of: datetime | None = None,
) -> SyncReport:
    """Run one provider sync in the given mode. Never fabricates prices or drops good data."""
    as_of = as_of or _now()
    report = SyncReport(provider=provider.provider_code, mode=mode.value)

    if settings.price_provider_kill_switch:
        report.reasons.append("kill_switch_on")
        return report

    products = list(provider.iterate_products(query or ProductQuery()))
    report.fetched = len(products)
    quality = evaluate_quality(products, settings, previous_count=previous_count)
    report.quality_status = quality.status
    report.coverage = quality.as_dict()

    if mode is SyncMode.DRY_RUN:
        report.reasons.append("dry_run_no_writes")
        return report

    if quality.status == "quarantined":  # a bad run never replaces the last good prices
        report.quarantined = True
        report.reasons.append("quarantined_" + ",".join(quality.reasons or []))
        return report

    if mode is SyncMode.PRODUCTION:
        guard_production_sync(db, provider.provider_code, settings)  # raises if not cleared

    run = CrawlRun(retailer_id=retailer.id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    staging = mode is SyncMode.STAGING
    metrics = RecordMetrics()
    for product in products:
        variant = _upsert_variant(db, retailer.id, product)
        _append_observation(db, retailer.id, variant, product, run.id, staging, as_of, metrics)
    # A "persisted observation" is a NEW economic fact; a re-confirmed fact is a reused occurrence.
    report.persisted_observations = metrics.observations_created
    report.observations_created = metrics.observations_created
    report.observations_reused = metrics.observations_reused
    report.occurrences_created = metrics.occurrences_created
    report.occurrences_reused = metrics.occurrences_reused
    report.run_id = str(run.public_id)
    report.reasons.append("staging_import" if staging else "production_import")
    return report


def _upsert_variant(
    db: Session, retailer_id: int, product: ExternalCatalogProduct
) -> ProductVariant:
    external = (
        db.execute(
            select(ExternalProduct).where(
                ExternalProduct.retailer_id == retailer_id,
                ExternalProduct.external_id == product.external_product_id,
            )
        )
        .scalars()
        .first()
    )
    now = _now()
    if external is None:
        external = ExternalProduct(
            retailer_id=retailer_id,
            external_id=product.external_product_id,
            first_seen_at=now,
            last_seen_at=now,
            active=True,
        )
        db.add(external)
        db.flush()
    if external.canonical_product_id is None:
        canonical = Product(
            retailer_id=retailer_id,
            external_id=product.external_product_id,
            name=product.product_name,
            brand=product.brand,
            package_quantity=product.net_content_quantity,
            package_unit=product.net_content_unit.value if product.net_content_unit else None,
            is_synthetic=False,
        )
        db.add(canonical)
        db.flush()
        external.canonical_product_id = canonical.id
        db.flush()
    variant = (
        db.execute(
            select(ProductVariant).where(
                ProductVariant.retailer_id == retailer_id,
                ProductVariant.external_product_id == external.id,
            )
        )
        .scalars()
        .first()
    )
    if variant is None:
        variant = ProductVariant(
            product_id=external.canonical_product_id,
            retailer_id=retailer_id,
            external_product_id=external.id,
            display_name=product.product_name,
            sell_unit=product.sell_unit.value,
            net_content_quantity=product.net_content_quantity,
            net_content_unit=product.net_content_unit.value if product.net_content_unit else None,
            active=True,
        )
        db.add(variant)
        db.flush()
    return variant


def _append_observation(
    db: Session,
    retailer_id: int,
    variant: ProductVariant,
    product: ExternalCatalogProduct,
    run_id: int,
    staging: bool,
    as_of: datetime,
    metrics: RecordMetrics,
) -> None:
    """Record one product's price.

    STAGING routes through the shared two-layer persistence (spec §3/§4): idempotency is by the
    16-field fact fingerprint, so a fact re-confirmed by another crawl/parser adds a provenance
    OCCURRENCE (never a duplicate observation) and a real change creates a new fact. This is the
    onboarding/discovery pipeline where the exact-duplicate observations accumulate.

    PRODUCTION keeps the established append-only current-price history (revalidate an unchanged
    open row in place, close it on a real change) so the production projection is unchanged.
    """
    scope = product.price_scope
    confidence = product.confidence_score or Decimal("1.0")
    if staging:
        candidate = PriceObservation(
            retailer_id=retailer_id,
            product_variant_id=variant.id,
            price_scope=scope.value,
            price_type=PriceType.REGULAR.value,
            amount=product.regular_price,
            currency=product.currency,
            available=True,
            observed_at=product.observed_at,
            imported_at=as_of,
            valid_from=product.observed_at,
            confidence_score=confidence,
            crawl_run_id=run_id,
            staging_only=True,
        )
        provenance = OccurrenceProvenance(
            provider_code=product.provider,
            crawl_run_id=run_id,
            confidence_score=confidence,
        )
        record_price_fact(db, candidate, provenance, imported_at=as_of, metrics=metrics)
        return

    # Production: append-only history (unchanged behavior).
    prior = (
        db.execute(
            select(PriceObservation)
            .where(
                PriceObservation.product_variant_id == variant.id,
                PriceObservation.store_id.is_(None),
                PriceObservation.price_scope == scope.value,
                PriceObservation.staging_only.is_(False),
                PriceObservation.valid_until.is_(None),
                PriceObservation.rolled_back_at.is_(None),
            )
            .order_by(PriceObservation.valid_from.desc())
        )
        .scalars()
        .first()
    )
    if prior is not None and prior.amount == product.regular_price:
        return  # unchanged price -> idempotent no-op
    if prior is not None and prior.valid_from <= product.observed_at:
        prior.valid_until = product.observed_at
        prior.closed_by_run_id = run_id
    db.add(
        PriceObservation(
            retailer_id=retailer_id,
            product_variant_id=variant.id,
            price_scope=scope.value,
            price_type=PriceType.REGULAR.value,
            amount=product.regular_price,
            currency=product.currency,
            available=True,
            observed_at=product.observed_at,
            imported_at=as_of,
            valid_from=product.observed_at,
            confidence_score=confidence,
            crawl_run_id=run_id,
            staging_only=False,
        )
    )
    metrics.observations_created += 1
    db.flush()


__all__ = ["SyncMode", "SyncReport", "run_provider_sync"]
