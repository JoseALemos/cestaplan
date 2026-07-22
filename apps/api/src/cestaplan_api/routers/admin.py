"""Admin router (prefix ``/api/v1/admin``): data imports and source status.

Every route requires a platform admin (:func:`cestaplan_api.deps.require_admin`); mutations
also require CSRF. Imports follow a two-phase flow: ``POST /imports`` validates a CSV/JSON
file and returns a preview (per-row errors + would-change summary) creating a ``DataImport``
that writes no prices; ``POST /imports/{id}/commit`` applies it; ``POST /imports/{id}/rollback``
removes the price rows it created. Money and quantities are returned as strings.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from cestaplan_api.adapters.registry import list_adapters
from cestaplan_api.deps import AdminUser, DbSession, verify_csrf
from cestaplan_api.models import DataImport, DataSource, Product, Store
from cestaplan_api.services import (
    commercial_feed_sync,
    enrichment,
    importer,
    ingredient_matching,
    open_prices_sync,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _format_from_upload(file: UploadFile) -> str:
    name = (file.filename or "").lower()
    if name.endswith(".csv") or file.content_type == "text/csv":
        return "csv"
    if name.endswith(".json") or file.content_type == "application/json":
        return "json"
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        detail="Formato no soportado: usa un fichero .csv o .json",
    )


def _parse_mapping(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"column_mapping no es JSON válido: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="column_mapping debe ser un objeto {columna_canonica: cabecera_origen}",
        )
    return parsed


def _import_detail(di: DataImport) -> dict[str, Any]:
    summary = di.summary or {}
    return {
        "id": str(di.public_id),
        "status": di.status,
        "format": di.format,
        "filename": di.filename,
        "source_type": di.source_type,
        "dry_run": di.dry_run,
        "checksum": di.checksum,
        "counts": {
            "row_count": di.row_count,
            "ok_count": di.ok_count,
            "error_count": di.error_count,
            "created": di.created_count,
            "updated": di.updated_count,
            "skipped": di.skipped_count,
        },
        "errors": summary.get("errors", []),
        "would_change": summary.get("sample", []),
        "created_at": di.created_at,
        "committed_at": di.committed_at,
        "rolled_back_at": di.rolled_back_at,
    }


def _import_summary_row(di: DataImport) -> dict[str, Any]:
    return {
        "id": str(di.public_id),
        "status": di.status,
        "format": di.format,
        "filename": di.filename,
        "dry_run": di.dry_run,
        "row_count": di.row_count,
        "error_count": di.error_count,
        "created": di.created_count,
        "updated": di.updated_count,
        "skipped": di.skipped_count,
        "created_at": di.created_at,
        "committed_at": di.committed_at,
    }


def _get_import(db: DbSession, import_id: uuid.UUID) -> DataImport:
    di = db.execute(
        select(DataImport).where(DataImport.public_id == import_id)
    ).scalar_one_or_none()
    if di is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Importación no encontrada")
    return di


# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
@router.post(
    "/imports",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def create_import(
    request: Request,
    admin: AdminUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Form()] = True,
    column_mapping: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Validate an uploaded CSV/JSON, return a preview and create a DataImport (no writes)."""
    fmt = _format_from_upload(file)
    mapping = _parse_mapping(column_mapping)
    content = file.file.read()
    di = importer.create_import(
        db,
        content=content,
        fmt=fmt,
        filename=file.filename,
        mapping=mapping,
        dry_run=dry_run,
        user_id=admin.id,
        ip=_client_ip(request),
    )
    db.flush()
    return _import_detail(di)


@router.get("/imports")
def list_imports(admin: AdminUser, db: DbSession) -> list[dict[str, Any]]:
    """List import batches, newest first."""
    rows = db.execute(
        select(DataImport).order_by(DataImport.created_at.desc())
    ).scalars().all()
    return [_import_summary_row(di) for di in rows]


@router.get("/imports/{import_id}")
def get_import(import_id: uuid.UUID, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Detail of one import batch, including per-row errors and the would-change sample."""
    return _import_detail(_get_import(db, import_id))


@router.post("/imports/{import_id}/commit", dependencies=[Depends(verify_csrf)])
def commit_import(
    request: Request, import_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Apply a validated import: write products/prices tagged with the import id."""
    di = _get_import(db, import_id)
    try:
        importer.commit_import(db, di, user_id=admin.id, ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.flush()
    return _import_detail(di)


@router.post("/imports/{import_id}/rollback", dependencies=[Depends(verify_csrf)])
def rollback_import(
    request: Request, import_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Remove the price observations a committed import created (prices only)."""
    di = _get_import(db, import_id)
    try:
        deleted = importer.rollback_import(db, di, user_id=admin.id, ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.flush()
    detail = _import_detail(di)
    detail["deleted_prices"] = deleted
    return detail


# --------------------------------------------------------------------------- #
# Open Food Facts enrichment (open_dataset — data only, never prices)
# --------------------------------------------------------------------------- #
class BarcodeIn(BaseModel):
    """Body of a barcode enrichment request."""

    barcode: str = Field(min_length=1, max_length=64)


class ProductEnrichIn(BaseModel):
    """Body of a per-product enrichment request (optional explicit barcode)."""

    barcode: str | None = Field(default=None, max_length=64)


def _enrichment_response(result: enrichment.EnrichmentResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "found": result.found,
        "applied": result.applied,
        "barcode": result.barcode,
        "product_public_id": result.product_public_id,
        "matched_products": result.matched_products,
        "message": result.message,
        "off_product": result.product,
        "attribution": result.attribution,
        "license_code": result.license_code,
        "source_url": result.source_url,
    }


def _refuse_if_disabled(result: enrichment.EnrichmentResult) -> None:
    if result.status == "disabled":
        raise HTTPException(status.HTTP_409_CONFLICT, detail=result.message)


@router.post("/enrich/barcode", dependencies=[Depends(verify_csrf)])
def enrich_barcode(
    body: BarcodeIn, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Dry OFF lookup for a barcode: returns the product data + ODbL attribution, no writes.

    Refused with 409 when the Open Food Facts source is disabled.
    """
    result = enrichment.enrich_product_by_barcode(db, body.barcode, apply=False)
    _refuse_if_disabled(result)
    return _enrichment_response(result)


@router.post(
    "/products/{product_id}/enrich",
    dependencies=[Depends(verify_csrf)],
)
def enrich_product_endpoint(
    product_id: uuid.UUID,
    body: ProductEnrichIn,
    admin: AdminUser,
    db: DbSession,
) -> dict[str, Any]:
    """Apply OFF enrichment to one product: writes barcode/nutrition/brand/image/category.

    Never reads or writes any price. Refused with 409 when OFF is disabled; 404 when the
    product is unknown.
    """
    product = db.execute(
        select(Product).where(
            Product.public_id == product_id, Product.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")

    result = enrichment.enrich_product(db, product, barcode=body.barcode, apply=True)
    _refuse_if_disabled(result)
    db.flush()
    return _enrichment_response(result)


# --------------------------------------------------------------------------- #
# Open Prices sync (open_dataset — real prices, ODbL)
# --------------------------------------------------------------------------- #
class OpenPricesSyncIn(BaseModel):
    """Body of an on-demand Open Prices sync (optional single store)."""

    store_id: uuid.UUID | None = Field(default=None)


@router.post(
    "/sources/open-prices/sync",
    dependencies=[Depends(verify_csrf)],
)
def sync_open_prices(
    body: OpenPricesSyncIn, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Pull real prices from Open Prices for all linked stores (or one) and append them.

    Gated by the Open Prices ``DataSource.is_enabled`` flag (409 when disabled). Returns a
    per-store summary plus the ODbL attribution. Idempotent + append-only (see the sync
    service); prices are real (``is_synthetic=False``), never fabricated.
    """
    if not open_prices_sync.open_prices_enabled(db):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="La fuente Open Prices está deshabilitada"
        )

    if body.store_id is not None:
        store = db.execute(
            select(Store).where(Store.public_id == body.store_id)
        ).scalar_one_or_none()
        if store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")
        stores = [store]
    else:
        stores = open_prices_sync.open_prices_stores(db)

    summaries = [open_prices_sync.sync_store(db, store) for store in stores]
    db.flush()
    return {
        "stores_synced": len(summaries),
        "inserted": sum(s.inserted for s in summaries),
        "fetched": sum(s.fetched for s in summaries),
        "results": [s.to_dict() for s in summaries],
        "attribution": open_prices_sync.OP_ATTRIBUTION_TEXT,
        "license_code": open_prices_sync.OP_LICENSE_CODE,
    }


# --------------------------------------------------------------------------- #
# Commercial feed sync (authorized_partner — licensed vendor prices, opt-in)
# --------------------------------------------------------------------------- #
class CommercialFeedSyncIn(BaseModel):
    """Body of an on-demand commercial-feed sync (optional single store)."""

    store_id: uuid.UUID | None = Field(default=None)


@router.post(
    "/sources/commercial-feed/sync",
    dependencies=[Depends(verify_csrf)],
)
def sync_commercial_feed(
    body: CommercialFeedSyncIn, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Pull real prices from the authorized commercial feed for all linked stores (or one).

    Gated by the commercial-feed ``DataSource.is_enabled`` flag **and** the connector being
    configured (base URL + key + mapping): a 409 is returned when disabled or unconfigured.
    Prices are real (``is_synthetic=False``, ``source_type='authorized_partner'``), append-only
    and idempotent; the operator licenses the feed and the vendor bears sourcing.
    """
    if not commercial_feed_sync.commercial_feed_enabled(db):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="El feed comercial está deshabilitado o sin configurar",
        )

    if body.store_id is not None:
        store = db.execute(
            select(Store).where(Store.public_id == body.store_id)
        ).scalar_one_or_none()
        if store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")
        stores = [store]
    else:
        stores = commercial_feed_sync.commercial_feed_stores(db)

    summaries = [
        commercial_feed_sync.sync_and_enrich_store(db, store) for store in stores
    ]
    db.flush()
    return {
        "stores_synced": len(summaries),
        "inserted": sum(s.inserted for s in summaries),
        "fetched": sum(s.fetched for s in summaries),
        "products_enriched": sum(s.products_enriched for s in summaries),
        "results": [s.to_dict() for s in summaries],
        "attribution": commercial_feed_sync.get_settings().commercial_feed_attribution,
        "license_code": commercial_feed_sync.get_settings().commercial_feed_license_code,
    }


class MapIngredientsIn(BaseModel):
    """Body of an on-demand ingredient-mapping run (optional single store)."""

    store_id: uuid.UUID | None = Field(default=None)


@router.post("/sources/map-ingredients", dependencies=[Depends(verify_csrf)])
def map_ingredients(
    body: MapIngredientsIn, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Map real chain products onto canonical ingredients and populate the mapping table.

    Conservative + idempotent: only clearly-correct matches are written (each with a
    confidence), and products already mapped are skipped. Returns the mapped count, a per-chain
    breakdown, sample mappings, and the resulting **chain-level** ingredient coverage (pricing
    is by chain, not by single store): how many canonical ingredients are now priced per chain.
    Never fabricates a price or a doubtful mapping.
    """
    store_id: int | None = None
    if body.store_id is not None:
        store = db.execute(
            select(Store).where(Store.public_id == body.store_id)
        ).scalar_one_or_none()
        if store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")
        store_id = store.id

    summary = ingredient_matching.map_real_products(db, store_id=store_id)
    summary.chain_coverage = ingredient_matching.all_chain_coverage(db)
    db.flush()
    return summary.to_dict()


@router.post("/sources/sync-all", dependencies=[Depends(verify_csrf)])
def sync_all_sources(admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Run the daily "sync everything" job: Open Prices sync → Open Food Facts enrichment.

    Gated by each source's ``DataSource.is_enabled`` flag — a disabled source is skipped (the
    response reflects which ran). Prices are real (``is_synthetic=False``, ODbL); OFF only ever
    contributes product data, never a price. The opt-in commercial feed (``authorized_partner``)
    is included only when it is enabled + configured. Returns the combined per-chain summary.
    """
    result = open_prices_sync.sync_all_and_enrich(db)
    payload = result.to_dict()
    # The authorized-partner feed only participates when explicitly enabled + configured.
    commercial = commercial_feed_sync.sync_all(db)
    payload["commercial_feed"] = commercial.to_dict()
    db.flush()
    return payload


# --------------------------------------------------------------------------- #
# Source / adapter status ("Estado de fuentes")
# --------------------------------------------------------------------------- #
@router.get("/sources")
def list_sources(admin: AdminUser, db: DbSession) -> list[dict[str, Any]]:
    """Status of every registered adapter joined with its DataSource and last import.

    For each adapter: key, source_type, enabled/disabled, community/skeleton status,
    license/attribution, and the most recent import batch (a coarse coverage signal).
    """
    sources_by_key: dict[str, DataSource] = {}
    for ds in db.execute(select(DataSource)).scalars().all():
        if ds.adapter_key:
            sources_by_key.setdefault(ds.adapter_key, ds)

    out: list[dict[str, Any]] = []
    for listing in list_adapters():
        ds = sources_by_key.get(listing.adapter_key)
        last_import: DataImport | None = None
        committed_batches = 0
        if ds is not None:
            last_import = db.execute(
                select(DataImport)
                .where(DataImport.data_source_id == ds.id)
                .order_by(DataImport.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            committed_batches = db.execute(
                select(func.count(DataImport.id)).where(
                    DataImport.data_source_id == ds.id,
                    DataImport.status == "committed",
                )
            ).scalar_one()

        out.append(
            {
                "adapter_key": listing.adapter_key,
                "version": listing.version,
                "source_type": listing.source_type,
                "status": listing.status.value,
                "enabled": listing.enabled,
                "is_community": listing.is_community,
                "requires_network": listing.requires_network,
                "retailers": list(listing.retailers),
                "capabilities": {
                    "search": listing.capabilities.supports_search,
                    "get_product": listing.capabilities.supports_get_product,
                    "get_price": listing.capabilities.supports_get_price,
                    "get_availability": listing.capabilities.supports_get_availability,
                    "store_catalog": listing.capabilities.supports_store_catalog,
                },
                "license_code": (ds.license_code if ds else listing.license_code),
                "attribution_text": (
                    ds.attribution_text if ds else listing.attribution_text
                ),
                "data_source": (
                    {
                        "slug": ds.slug,
                        "name": ds.name,
                        "is_enabled": ds.is_enabled,
                        "url": ds.url,
                    }
                    if ds
                    else None
                ),
                "last_import": (
                    {
                        "id": str(last_import.public_id),
                        "status": last_import.status,
                        "created_at": last_import.created_at,
                        "committed_at": last_import.committed_at,
                    }
                    if last_import
                    else None
                ),
                "committed_import_batches": committed_batches,
            }
        )
    return out
