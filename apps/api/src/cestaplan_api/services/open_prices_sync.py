"""Open Prices sync service — pull real prices for a store and append observations.

This is the automatic-update core. :func:`sync_store` pulls every Open Prices observation
for one ``Store`` (by its OSM location, paginated) and, per priced barcode, idempotently
upserts a real ``Product`` + ``ProductBarcode`` and **appends** a ``ProductPrice``
observation tagged with a per-run ``DataImport`` batch.

Design guarantees (mirroring docs/DATA_SOURCES.md and the importer):

- **Real only, never fabricated.** Amounts are :class:`~decimal.Decimal` straight from the
  API; a row without a barcode or a usable price/date is skipped, never invented.
- **Append-only + idempotent.** A ``(store, product, observed_at)`` observation already
  present is skipped, so re-syncing the same day never duplicates price history.
- **Provenance.** Every written price carries ``source_type='open_dataset'``,
  ``is_synthetic=False``, the Open Prices price-page ``source_url`` and ODbL attribution.
- **Graceful.** Network/HTTP/parse errors surface as a partial result (whatever was
  gathered) plus a logged error string; the sync never crashes the caller.

The service flushes but does not commit; the command / admin endpoint owns the transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openfoodfacts import OpenFoodFactsAdapter
from cestaplan_api.adapters.openprices import (
    OP_ADAPTER_KEY,
    OP_ATTRIBUTION_TEXT,
    OP_DATA_SOURCE_SLUG,
    OP_LICENSE_CODE,
    OP_SITE_URL,
    OP_SOURCE_NAME,
    OpenPrice,
    OpenPricesAdapter,
)
from cestaplan_api.models import (
    DataImport,
    DataSource,
    Product,
    ProductBarcode,
    ProductNutrition,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services.enrichment import enrich_product, off_source_enabled

logger = logging.getLogger(__name__)

#: Default confidence for an open-dataset observation (importer §4.1 midpoint).
_OP_CONFIDENCE = Decimal("0.5000")


@dataclass(slots=True)
class SyncSummary:
    """Outcome of syncing one store from Open Prices."""

    store_public_id: str
    osm_id: int | None = None
    osm_type: str | None = None
    fetched: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    skipped_no_barcode: int = 0
    products_created: int = 0
    barcodes_created: int = 0
    products_enriched: int = 0
    #: Product ids that carried a priced barcode this run (candidates for OFF enrichment).
    #: Internal bookkeeping — not part of the serialized summary.
    touched_product_ids: set[int] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    attribution: str = OP_ATTRIBUTION_TEXT
    license_code: str = OP_LICENSE_CODE

    def to_dict(self) -> dict[str, object]:
        return {
            "store_id": self.store_public_id,
            "osm_id": self.osm_id,
            "osm_type": self.osm_type,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "skipped_existing": self.skipped_existing,
            "skipped_no_barcode": self.skipped_no_barcode,
            "products_created": self.products_created,
            "barcodes_created": self.barcodes_created,
            "products_enriched": self.products_enriched,
            "errors": list(self.errors),
            "attribution": self.attribution,
            "license_code": self.license_code,
        }


# --------------------------------------------------------------------------- #
# Open Prices DataSource row (ODbL) — ensured to exist, gated by is_enabled
# --------------------------------------------------------------------------- #
def ensure_open_prices_data_source(db: Session) -> DataSource:
    """Return the Open Prices ``DataSource`` row, creating it (enabled) if absent.

    Never overwrites an existing row's ``is_enabled`` — an admin who disabled Open Prices
    stays in control.
    """
    ds = db.execute(
        select(DataSource).where(DataSource.adapter_key == OP_ADAPTER_KEY)
    ).scalar_one_or_none()
    if ds is None:
        ds = DataSource(
            slug=OP_DATA_SOURCE_SLUG,
            name=OP_SOURCE_NAME,
            source_type="open_dataset",
            adapter_key=OP_ADAPTER_KEY,
            license_code=OP_LICENSE_CODE,
            attribution_text=OP_ATTRIBUTION_TEXT,
            is_enabled=True,
            url=OP_SITE_URL,
        )
        db.add(ds)
        db.flush()
    return ds


def open_prices_enabled(db: Session) -> bool:
    """Whether the Open Prices source is enabled (ensuring its row exists first)."""
    return ensure_open_prices_data_source(db).is_enabled


# --------------------------------------------------------------------------- #
# OSM location <-> external_code
# --------------------------------------------------------------------------- #
def store_external_code(osm_type: str, osm_id: int) -> str:
    """Canonical ``external_code`` for an OSM-located store: ``osm:{TYPE}/{id}``."""
    return f"osm:{osm_type.upper()}/{osm_id}"


def parse_osm_from_external_code(external_code: str | None) -> tuple[int, str] | None:
    """Recover ``(osm_id, osm_type)`` from an ``osm:{TYPE}/{id}`` external code, or ``None``."""
    if not external_code or not external_code.startswith("osm:"):
        return None
    try:
        osm_type, osm_id = external_code[4:].split("/", 1)
        return int(osm_id), osm_type.upper()
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def _get_or_create_product(
    db: Session, retailer: Retailer, barcode: str, product_name: str | None
) -> tuple[Product, bool]:
    """Resolve the retailer's product for ``barcode`` (by barcode, then external_id).

    Creates a real (``is_synthetic=False``) product keyed on the barcode when none exists.
    The name is the observed product name or a neutral ``Producto {barcode}`` fallback —
    never fabricated attributes beyond that.
    """
    product = db.execute(
        select(Product)
        .join(ProductBarcode, ProductBarcode.product_id == Product.id)
        .where(
            Product.retailer_id == retailer.id,
            ProductBarcode.barcode == barcode,
            Product.deleted_at.is_(None),
        )
    ).scalars().first()
    if product is not None:
        return product, False

    product = db.execute(
        select(Product).where(
            Product.retailer_id == retailer.id, Product.external_id == barcode
        )
    ).scalar_one_or_none()
    if product is not None:
        return product, False

    product = Product(
        retailer_id=retailer.id,
        external_id=barcode,
        name=product_name or f"Producto {barcode}",
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


def _package_from_price_per(
    price_per: str | None, amount: Decimal
) -> tuple[Decimal, str, Decimal | None]:
    """Map Open Prices ``price_per`` to (package_quantity, package_unit, unit_price).

    Open Prices reports a per-unit or per-kilogram basis for loose/category items; packaged
    (barcoded) items usually carry no basis. We record a package of one base unit priced at
    ``amount`` and derive ``unit_price`` only when the basis is meaningful — otherwise it
    stays ``None`` (never fabricated).
    """
    basis = (price_per or "").strip().upper()
    if basis in ("KILOGRAM", "KG"):
        return Decimal("1"), "kg", amount  # amount is €/kg
    if basis == "UNIT":
        return Decimal("1"), "unit", amount  # amount is €/unit
    return Decimal("1"), "unit", None  # unknown basis → package price only, no €/base


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


def _promotion_note(price: OpenPrice) -> str | None:
    if not price.price_is_discounted:
        return None
    if price.price_without_discount is not None:
        return f"Precio con descuento (antes {price.price_without_discount})"
    return "Precio con descuento"


# --------------------------------------------------------------------------- #
# Sync one store
# --------------------------------------------------------------------------- #
def sync_store(
    db: Session,
    store: Store,
    *,
    adapter: OpenPricesAdapter | None = None,
    now: datetime | None = None,
) -> SyncSummary:
    """Pull Open Prices for ``store`` and append new price observations.

    Idempotent and append-only: an observation already present ``(store, product,
    observed_at)`` is skipped. Every price written is real (``is_synthetic=False``,
    ``source_type='open_dataset'``), Decimal money, ODbL-attributed and tagged with a
    per-run ``DataImport`` batch. Errors are collected (partial success), never raised.
    """
    now = now or datetime.now(UTC)
    summary = SyncSummary(store_public_id=str(store.public_id))

    osm = parse_osm_from_external_code(store.external_code)
    if osm is None:
        summary.errors.append(
            f"external_code no es una ubicación OSM válida: {store.external_code!r}"
        )
        return summary
    osm_id, osm_type = osm
    summary.osm_id, summary.osm_type = osm_id, osm_type

    retailer = db.get(Retailer, store.retailer_id)
    if retailer is None:
        summary.errors.append("la tienda no tiene distribuidor asociado")
        return summary

    adapter = adapter or OpenPricesAdapter()
    try:
        prices = adapter.fetch_store_prices(osm_id, osm_type)
    except Exception as exc:  # defensive: adapter degrades gracefully, but never crash here
        logger.warning("Open Prices fetch failed for store %s: %s", store.public_id, exc)
        summary.errors.append(f"fallo al consultar Open Prices: {exc}")
        return summary
    summary.fetched = len(prices)

    if not prices:
        return summary

    data_source = ensure_open_prices_data_source(db)
    batch = DataImport(
        retailer_id=retailer.id,
        data_source_id=data_source.id,
        source_type="open_dataset",
        status="committed",
        filename=f"open-prices:{store_external_code(osm_type, osm_id)}",
        format="json",
        row_count=len(prices),
        dry_run=False,
        committed_at=now,
    )
    db.add(batch)
    db.flush()

    for price in prices:
        if not price.barcode:
            summary.skipped_no_barcode += 1
            continue
        observed_at = datetime.combine(
            price.observed_on, datetime.min.time(), tzinfo=UTC
        )
        product, created = _get_or_create_product(
            db, retailer, price.barcode, price.product_name
        )
        if created:
            summary.products_created += 1
        if _ensure_barcode(db, product, price.barcode):
            summary.barcodes_created += 1
        summary.touched_product_ids.add(product.id)
        # Flush so the just-created product/barcode is visible to the dedup SELECTs of
        # later prices in this same batch (append-only history is preserved).
        db.flush()

        if _observation_exists(db, store, product, observed_at):
            summary.skipped_existing += 1
            continue

        package_quantity, package_unit, unit_price = _package_from_price_per(
            price.price_per, price.amount
        )
        db.add(
            ProductPrice(
                retailer_id=retailer.id,
                store_id=store.id,
                product_id=product.id,
                amount=price.amount,
                currency=price.currency,
                package_quantity=package_quantity,
                package_unit=package_unit,
                unit_price=unit_price,
                promotion=_promotion_note(price),
                availability=None,
                source_type="open_dataset",
                source_name=OP_SOURCE_NAME,
                source_url=price.source_url,
                observed_at=observed_at,
                imported_at=now,
                expires_at=None,
                confidence_score=_OP_CONFIDENCE,
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
    batch.skipped_count = summary.skipped_existing + summary.skipped_no_barcode
    batch.ok_count = summary.inserted
    batch.summary = summary.to_dict()
    db.flush()
    return summary


# --------------------------------------------------------------------------- #
# Sync many stores
# --------------------------------------------------------------------------- #
def open_prices_stores(db: Session) -> list[Store]:
    """All active stores whose retailer is linked to the Open Prices adapter."""
    return list(
        db.execute(
            select(Store)
            .join(Retailer, Retailer.id == Store.retailer_id)
            .where(
                Retailer.adapter_key == OP_ADAPTER_KEY,
                Store.is_active.is_(True),
            )
            .order_by(Store.id)
        ).scalars().all()
    )


def sync_all(
    db: Session, *, adapter: OpenPricesAdapter | None = None
) -> list[SyncSummary]:
    """Sync every Open-Prices-linked store. Returns one :class:`SyncSummary` per store."""
    adapter = adapter or OpenPricesAdapter()
    return [sync_store(db, store, adapter=adapter) for store in open_prices_stores(db)]


# --------------------------------------------------------------------------- #
# OFF enrichment of the real products Open Prices created ("data nutrition")
# --------------------------------------------------------------------------- #
def _product_has_nutrition(db: Session, product_id: int) -> bool:
    return (
        db.execute(
            select(ProductNutrition.id).where(ProductNutrition.product_id == product_id)
        ).first()
        is not None
    )


def enrich_products(
    db: Session,
    product_ids: set[int] | list[int],
    *,
    off_adapter: OpenFoodFactsAdapter | None = None,
    skip_enriched: bool = True,
) -> int:
    """Enrich the given real products from Open Food Facts (data only, **never prices**).

    Reuses the enrichment service to fill nutrition/allergens/brand/image/category where OFF
    has the barcode. Idempotent and graceful: an OFF 404/network miss leaves the product
    un-enriched (no crash). When ``skip_enriched`` is set, products that already carry a
    ``ProductNutrition`` row are left untouched (keeps the daily cron cheap and respectful).
    Returns the number of products where OFF data was applied.
    """
    if not off_source_enabled(db):
        return 0
    off_adapter = off_adapter or OpenFoodFactsAdapter()
    applied = 0
    for pid in product_ids:
        if skip_enriched and _product_has_nutrition(db, pid):
            continue
        product = db.get(Product, pid)
        if product is None or product.deleted_at is not None:
            continue
        result = enrich_product(db, product, apply=True, adapter=off_adapter)
        if result.applied:
            applied += 1
    return applied


def sync_and_enrich_store(
    db: Session,
    store: Store,
    *,
    op_adapter: OpenPricesAdapter | None = None,
    off_adapter: OpenFoodFactsAdapter | None = None,
    enrich: bool = True,
    now: datetime | None = None,
) -> SyncSummary:
    """Sync one store's Open Prices, then (optionally) OFF-enrich the products it touched.

    ``enrich`` defaults on (the CLI/cron path). Enrichment is gated by the OFF source flag,
    idempotent and graceful — prices are never taken from OFF. Returns the same
    :class:`SyncSummary`, with ``products_enriched`` set.
    """
    summary = sync_store(db, store, adapter=op_adapter, now=now)
    if enrich and summary.touched_product_ids and off_source_enabled(db):
        summary.products_enriched = enrich_products(
            db, summary.touched_product_ids, off_adapter=off_adapter
        )
    return summary


# --------------------------------------------------------------------------- #
# "Sync everything" orchestration (Open Prices → OFF enrichment), gated by flags
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class OrchestrationSummary:
    """Combined outcome of the daily "sync everything" job across the open sources."""

    open_prices_enabled: bool = True
    openfoodfacts_enabled: bool = True
    stores_synced: int = 0
    prices_fetched: int = 0
    prices_inserted: int = 0
    products_created: int = 0
    products_enriched: int = 0
    #: {ChainName: {stores, prices_inserted, products_enriched}} for the per-chain report.
    per_chain: dict[str, dict[str, int]] = field(default_factory=dict)
    store_results: list[dict[str, object]] = field(default_factory=list)
    attribution: str = OP_ATTRIBUTION_TEXT
    license_code: str = OP_LICENSE_CODE

    def to_dict(self) -> dict[str, object]:
        return {
            "open_prices_enabled": self.open_prices_enabled,
            "openfoodfacts_enabled": self.openfoodfacts_enabled,
            "stores_synced": self.stores_synced,
            "prices_fetched": self.prices_fetched,
            "prices_inserted": self.prices_inserted,
            "products_created": self.products_created,
            "products_enriched": self.products_enriched,
            "per_chain": {k: dict(v) for k, v in self.per_chain.items()},
            "store_results": list(self.store_results),
            "attribution": self.attribution,
            "license_code": self.license_code,
        }


def sync_all_and_enrich(
    db: Session,
    *,
    op_adapter: OpenPricesAdapter | None = None,
    off_adapter: OpenFoodFactsAdapter | None = None,
    enrich: bool = True,
) -> OrchestrationSummary:
    """Run the whole daily job: Open Prices sync for every linked store → OFF enrichment.

    Gated by the ``DataSource.is_enabled`` flags: a disabled Open Prices source skips the sync
    entirely; a disabled Open Food Facts source (or ``enrich=False``) skips enrichment. Prices
    are never taken from OFF. Returns a combined, per-chain :class:`OrchestrationSummary`.
    """
    result = OrchestrationSummary()
    result.open_prices_enabled = open_prices_enabled(db)
    result.openfoodfacts_enabled = off_source_enabled(db)
    if not result.open_prices_enabled:
        return result

    op_adapter = op_adapter or OpenPricesAdapter()
    do_enrich = enrich and result.openfoodfacts_enabled
    if do_enrich:
        off_adapter = off_adapter or OpenFoodFactsAdapter()

    for store in open_prices_stores(db):
        summary = sync_and_enrich_store(
            db,
            store,
            op_adapter=op_adapter,
            off_adapter=off_adapter if do_enrich else None,
            enrich=do_enrich,
        )
        retailer = db.get(Retailer, store.retailer_id)
        chain = retailer.name if retailer is not None else "?"
        bucket = result.per_chain.setdefault(
            chain, {"stores": 0, "prices_inserted": 0, "products_enriched": 0}
        )
        bucket["stores"] += 1
        bucket["prices_inserted"] += summary.inserted
        bucket["products_enriched"] += summary.products_enriched

        result.stores_synced += 1
        result.prices_fetched += summary.fetched
        result.prices_inserted += summary.inserted
        result.products_created += summary.products_created
        result.products_enriched += summary.products_enriched
        result.store_results.append(summary.to_dict())
    return result
