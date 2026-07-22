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
from cestaplan_api.models import DataImport, DataSource, Product
from cestaplan_api.services import enrichment, importer

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
