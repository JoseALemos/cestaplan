"""Commercial-feed sync service — pull licensed prices and append observations.

Mirrors ``open_prices_sync``: :func:`sync_commercial_feed` pulls the priced products a paid,
authorized third-party feed exposes for one ``Store`` (config-driven, via
:class:`~cestaplan_api.adapters.commercial_feed.CommercialFeedAdapter`) and, per row,
idempotently upserts a **real** ``Product`` + ``ProductBarcode`` and **appends** a
``ProductPrice`` observation tagged ``source_type='authorized_partner'``.

Design guarantees:

- **Opt-in.** Runs only when the ``commercial-feed`` ``DataSource.is_enabled`` flag is on **and**
  the connector is configured (base URL + key + mapping). Otherwise it is a clear no-op.
- **Real only, never fabricated.** Amounts are :class:`~decimal.Decimal` from the feed; a row
  without a usable price or product identity is skipped by the adapter, never invented.
- **Append-only + idempotent.** A ``(store, product, observed_at)`` observation already present
  is skipped, so re-syncing the same day never duplicates price history.
- **Provenance.** Every written price carries ``source_type='authorized_partner'``,
  ``is_synthetic=False``, the configured source name and the operator's attribution.
- **Graceful.** Adapter degradation surfaces as a partial result plus a logged error string;
  the sync never crashes the caller.

The service flushes but does not commit; the command / admin endpoint owns the transaction.
Optional OFF enrichment (data only, never prices) reuses ``open_prices_sync.enrich_products``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.commercial_feed import (
    CF_ADAPTER_KEY,
    CF_DATA_SOURCE_SLUG,
    CF_SOURCE_TYPE,
    CommercialFeedAdapter,
)
from cestaplan_api.config import Settings, get_settings
from cestaplan_api.models import (
    DataImport,
    DataSource,
    Product,
    ProductBarcode,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services.open_prices_sync import enrich_products

logger = logging.getLogger(__name__)

#: Default confidence for an authorized-partner observation (a licensed vendor feed; the
#: importer §4.1 range treats partner data as high-trust but still machine-unverified here).
_CF_CONFIDENCE = Decimal("0.7000")


@dataclass(slots=True)
class CommercialFeedSummary:
    """Outcome of syncing one store from the commercial feed."""

    store_public_id: str
    fetched: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    skipped_no_identity: int = 0
    products_created: int = 0
    barcodes_created: int = 0
    products_enriched: int = 0
    #: Product ids touched this run (candidates for optional OFF enrichment). Internal.
    touched_product_ids: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    attribution: str = ""
    license_code: str = "proprietary"

    def to_dict(self) -> dict[str, object]:
        return {
            "store_id": self.store_public_id,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped_existing": self.skipped_existing,
            "skipped_no_identity": self.skipped_no_identity,
            "products_created": self.products_created,
            "barcodes_created": self.barcodes_created,
            "products_enriched": self.products_enriched,
            "errors": list(self.errors),
            "attribution": self.attribution,
            "license_code": self.license_code,
        }


# --------------------------------------------------------------------------- #
# DataSource row (authorized_partner) — ensured to exist, DISABLED by default
# --------------------------------------------------------------------------- #
def ensure_commercial_feed_data_source(
    db: Session, settings: Settings | None = None
) -> DataSource:
    """Return the commercial-feed ``DataSource`` row, creating it **disabled** if absent.

    Never overwrites an existing row's ``is_enabled`` — the operator stays in control. The row
    is created ``is_enabled=False`` (opt-in): enabling it is a deliberate admin action.
    """
    settings = settings or get_settings()
    ds = db.execute(
        select(DataSource).where(DataSource.adapter_key == CF_ADAPTER_KEY)
    ).scalar_one_or_none()
    if ds is None:
        ds = DataSource(
            slug=CF_DATA_SOURCE_SLUG,
            name=settings.commercial_feed_source_name,
            source_type=CF_SOURCE_TYPE,
            adapter_key=CF_ADAPTER_KEY,
            license_code=settings.commercial_feed_license_code,
            attribution_text=settings.commercial_feed_attribution,
            is_enabled=False,
            url=settings.commercial_feed_base_url or None,
        )
        db.add(ds)
        db.flush()
    return ds


def commercial_feed_enabled(db: Session, settings: Settings | None = None) -> bool:
    """Whether the commercial feed may run: DataSource enabled **and** connector configured."""
    settings = settings or get_settings()
    ds = ensure_commercial_feed_data_source(db, settings)
    return bool(ds.is_enabled and settings.commercial_feed_configured)


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def _get_or_create_product(
    db: Session, retailer: Retailer, external_id: str, product_name: str | None
) -> tuple[Product, bool]:
    """Resolve the retailer's product for ``external_id`` (barcode or provider ref).

    Creates a real (``is_synthetic=False``) product keyed on the feed's stable identity when
    none exists. The name is the observed name or a neutral fallback — never fabricated beyond.
    """
    product = db.execute(
        select(Product)
        .join(ProductBarcode, ProductBarcode.product_id == Product.id)
        .where(
            Product.retailer_id == retailer.id,
            ProductBarcode.barcode == external_id,
            Product.deleted_at.is_(None),
        )
    ).scalars().first()
    if product is not None:
        return product, False

    product = db.execute(
        select(Product).where(
            Product.retailer_id == retailer.id, Product.external_id == external_id
        )
    ).scalar_one_or_none()
    if product is not None:
        return product, False

    product = Product(
        retailer_id=retailer.id,
        external_id=external_id,
        name=product_name or f"Producto {external_id}",
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    return product, True


def _ensure_barcode(db: Session, product: Product, barcode: str) -> bool:
    """Attach ``barcode`` to ``product`` once (idempotent). Returns True if newly added."""
    exists = db.execute(
        select(ProductBarcode.id).where(
            ProductBarcode.product_id == product.id,
            ProductBarcode.barcode == barcode,
        )
    ).first()
    if exists:
        return False
    has_any = db.execute(
        select(ProductBarcode.id).where(ProductBarcode.product_id == product.id)
    ).first()
    db.add(
        ProductBarcode(product_id=product.id, barcode=barcode, is_primary=has_any is None)
    )
    return True


def _observation_exists(
    db: Session, store: Store, product: Product, observed_at: datetime
) -> bool:
    return (
        db.execute(
            select(ProductPrice.id).where(
                ProductPrice.store_id == store.id,
                ProductPrice.product_id == product.id,
                ProductPrice.observed_at == observed_at,
            )
        ).first()
        is not None
    )


# --------------------------------------------------------------------------- #
# Sync one store
# --------------------------------------------------------------------------- #
def commercial_feed_stores(db: Session) -> list[Store]:
    """All active stores whose retailer is linked to the commercial-feed adapter."""
    return list(
        db.execute(
            select(Store)
            .join(Retailer, Retailer.id == Store.retailer_id)
            .where(
                Retailer.adapter_key == CF_ADAPTER_KEY,
                Store.is_active.is_(True),
            )
            .order_by(Store.id)
        ).scalars().all()
    )


def sync_commercial_feed(
    db: Session,
    store: Store,
    *,
    adapter: CommercialFeedAdapter | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> CommercialFeedSummary:
    """Pull the commercial feed for ``store`` and append new price observations.

    Idempotent and append-only. Every price written is real (``is_synthetic=False``,
    ``source_type='authorized_partner'``), Decimal money, attributed to the operator's licensed
    source and tagged with a per-run ``DataImport`` batch. Errors are collected (partial
    success), never raised. Date-less feed rows are observed at midnight UTC of ``now`` so a
    daily cron produces exactly one observation per product per day.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    day = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    summary = CommercialFeedSummary(
        store_public_id=str(store.public_id),
        attribution=settings.commercial_feed_attribution,
        license_code=settings.commercial_feed_license_code,
    )

    retailer = db.get(Retailer, store.retailer_id)
    if retailer is None:
        summary.errors.append("la tienda no tiene distribuidor asociado")
        return summary

    adapter = adapter or CommercialFeedAdapter()
    try:
        records = adapter.fetch_products(
            retailer_slug=retailer.slug,
            store_external_code=store.external_code or f"store:{store.public_id}",
            default_observed_at=day,
        )
    except Exception as exc:  # defensive: adapter degrades gracefully, but never crash here
        logger.warning("Commercial feed fetch failed for store %s: %s", store.public_id, exc)
        summary.errors.append(f"fallo al consultar el feed comercial: {exc}")
        return summary
    summary.fetched = len(records)

    if not records:
        return summary

    data_source = ensure_commercial_feed_data_source(db, settings)
    batch = DataImport(
        retailer_id=retailer.id,
        data_source_id=data_source.id,
        source_type=CF_SOURCE_TYPE,
        status="committed",
        filename=f"commercial-feed:{retailer.slug}:{store.public_id}",
        format="json",
        row_count=len(records),
        dry_run=False,
        committed_at=now,
    )
    db.add(batch)
    db.flush()

    for record in records:
        if not record.product_external_id:
            summary.skipped_no_identity += 1
            continue
        product, created = _get_or_create_product(
            db, retailer, record.product_external_id, record.product_name
        )
        if created:
            summary.products_created += 1
        if record.barcode and _ensure_barcode(db, product, record.barcode):
            summary.barcodes_created += 1
        summary.touched_product_ids.add(product.id)
        # Flush so a product created earlier in this batch is visible to later dedup SELECTs.
        db.flush()

        if _observation_exists(db, store, product, record.observed_at):
            summary.skipped_existing += 1
            continue

        db.add(
            ProductPrice(
                retailer_id=retailer.id,
                store_id=store.id,
                product_id=product.id,
                amount=record.amount,
                currency=record.currency,
                package_quantity=record.package_quantity,
                package_unit=record.package_unit,
                unit_price=record.unit_price,
                promotion=record.promotion,
                availability=record.availability,
                source_type=CF_SOURCE_TYPE,
                source_name=record.source_name,
                source_url=record.source_url,
                observed_at=record.observed_at,
                imported_at=now,
                expires_at=record.expires_at,
                confidence_score=_CF_CONFIDENCE,
                import_id=batch.id,
                verification_status="unverified",
                is_synthetic=False,
            )
        )
        summary.inserted += 1
        db.flush()

    store.catalog_updated_at = now
    batch.created_count = summary.products_created
    batch.updated_count = summary.inserted
    batch.skipped_count = summary.skipped_existing + summary.skipped_no_identity
    batch.ok_count = summary.inserted
    batch.summary = summary.to_dict()
    db.flush()
    return summary


def sync_and_enrich_store(
    db: Session,
    store: Store,
    *,
    adapter: CommercialFeedAdapter | None = None,
    settings: Settings | None = None,
    enrich: bool = True,
    now: datetime | None = None,
) -> CommercialFeedSummary:
    """Sync one store's commercial feed, then optionally OFF-enrich the products it touched.

    Enrichment is gated by the OFF source flag, idempotent and graceful — prices are never taken
    from OFF. Returns the same :class:`CommercialFeedSummary`, with ``products_enriched`` set.
    """
    summary = sync_commercial_feed(
        db, store, adapter=adapter, settings=settings, now=now
    )
    if enrich and summary.touched_product_ids:
        summary.products_enriched = enrich_products(db, summary.touched_product_ids)
    return summary


# --------------------------------------------------------------------------- #
# Sync many stores (gated)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CommercialFeedRun:
    """Combined outcome of a commercial-feed run across every linked store."""

    enabled: bool = False
    configured: bool = False
    stores_synced: int = 0
    prices_fetched: int = 0
    prices_inserted: int = 0
    products_created: int = 0
    products_enriched: int = 0
    store_results: list[dict[str, object]] = field(default_factory=list)
    attribution: str = ""
    license_code: str = "proprietary"

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "stores_synced": self.stores_synced,
            "prices_fetched": self.prices_fetched,
            "prices_inserted": self.prices_inserted,
            "products_created": self.products_created,
            "products_enriched": self.products_enriched,
            "store_results": list(self.store_results),
            "attribution": self.attribution,
            "license_code": self.license_code,
        }


def sync_all(
    db: Session,
    *,
    adapter: CommercialFeedAdapter | None = None,
    settings: Settings | None = None,
    enrich: bool = True,
) -> CommercialFeedRun:
    """Run the commercial-feed sync for every linked store, gated by enablement + config.

    A disabled or unconfigured source skips the sync entirely (the result reflects why). OFF
    enrichment (data only, never prices) runs when its own source flag is on. Returns a combined
    :class:`CommercialFeedRun` summary.
    """
    settings = settings or get_settings()
    result = CommercialFeedRun(
        configured=settings.commercial_feed_configured,
        attribution=settings.commercial_feed_attribution,
        license_code=settings.commercial_feed_license_code,
    )
    result.enabled = commercial_feed_enabled(db, settings)
    if not result.enabled:
        return result

    adapter = adapter or CommercialFeedAdapter(config=None)
    for store in commercial_feed_stores(db):
        summary = sync_and_enrich_store(
            db, store, adapter=adapter, settings=settings, enrich=enrich
        )
        result.stores_synced += 1
        result.prices_fetched += summary.fetched
        result.prices_inserted += summary.inserted
        result.products_created += summary.products_created
        result.products_enriched += summary.products_enriched
        result.store_results.append(summary.to_dict())
    return result
