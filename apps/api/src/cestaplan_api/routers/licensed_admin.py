"""Admin API for licensed-catalog import, mapping review and coverage (FASE 4).

Prefix ``/api/v1/admin/licensed``. Every route requires an admin session; mutations require
CSRF. This exposes the FASE 2/3 machinery to operators:

- ``POST/GET /field-mappings`` — manage provider-agnostic :class:`SupplierFieldMapping`s.
- ``POST /sample-import`` — upload a licensed sample and run the 10-step pipeline (dry-run by
  default), returning the full :class:`SampleImportReport`.
- ``GET /review-queue`` + ``POST /review/{id}/approve|reject`` — the manual review queue:
  machine candidates are inactive/unverified until a human approves them (only then are they
  active and ``human_verified``), or rejects them (``disputed``, inactive).
- ``GET /coverage`` — per-retailer costable/ingredient-coverage metrics (FASE 5 gate input).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, select

from cestaplan_api.deps import AdminUser, DbSession, verify_csrf
from cestaplan_api.ingestion.licensed_catalog import (
    COSTABLE_UNITS,
    CsvLicensedCatalogImporter,
    JsonLicensedCatalogImporter,
    SupplierFieldMap,
)
from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    ProductVariant,
    Retailer,
    SupplierFieldMapping,
)
from cestaplan_api.services.readiness import GateConfig, evaluate_readiness
from cestaplan_api.services.sample_import import run_sample_import

router = APIRouter(prefix="/api/v1/admin/licensed", tags=["admin", "licensed"])


def _now() -> datetime:
    return datetime.now(UTC)


def _get_retailer(db: DbSession, retailer_id: uuid.UUID) -> Retailer:
    retailer = db.execute(
        select(Retailer).where(Retailer.public_id == retailer_id)
    ).scalar_one_or_none()
    if retailer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Distribuidor no encontrado")
    return retailer


# --------------------------------------------------------------------------- #
# Supplier field mappings
# --------------------------------------------------------------------------- #
class FieldMappingIn(BaseModel):
    source_name: str
    field_map: dict[str, str]
    unit_aliases: dict[str, str] | None = None
    default_currency: str | None = None
    notes: str | None = None


def _field_mapping_row(m: SupplierFieldMapping) -> dict[str, Any]:
    return {
        "id": str(m.public_id),
        "source_name": m.source_name,
        "field_map": m.field_map,
        "unit_aliases": m.unit_aliases,
        "default_currency": m.default_currency,
        "is_active": m.is_active,
        "notes": m.notes,
    }


@router.post(
    "/field-mappings", status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_csrf)]
)
def create_field_mapping(
    body: FieldMappingIn, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Register a provider-agnostic supplier field map. Rejects a duplicate source_name."""
    exists = db.execute(
        select(SupplierFieldMapping.id).where(
            SupplierFieldMapping.source_name == body.source_name
        )
    ).first()
    if exists is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Ya existe un mapeo con ese source_name"
        )
    mapping = SupplierFieldMapping(
        source_name=body.source_name,
        field_map=body.field_map,
        unit_aliases=body.unit_aliases,
        default_currency=body.default_currency,
        notes=body.notes,
        is_active=True,
    )
    db.add(mapping)
    db.flush()
    return _field_mapping_row(mapping)


@router.get("/field-mappings")
def list_field_mappings(admin: AdminUser, db: DbSession) -> list[dict[str, Any]]:
    rows = db.execute(
        select(SupplierFieldMapping).order_by(SupplierFieldMapping.source_name)
    ).scalars().all()
    return [_field_mapping_row(m) for m in rows]


# --------------------------------------------------------------------------- #
# Sample import
# --------------------------------------------------------------------------- #
def _resolve_field_map(
    db: DbSession, field_mapping_id: uuid.UUID | None, field_map_json: str | None
) -> SupplierFieldMap:
    if field_mapping_id is not None:
        row = db.execute(
            select(SupplierFieldMapping).where(
                SupplierFieldMapping.public_id == field_mapping_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Mapeo no encontrado")
        return SupplierFieldMap(
            field_map=dict(row.field_map),
            unit_aliases=dict(row.unit_aliases or {}),
            default_currency=row.default_currency,
        )
    if field_map_json:
        try:
            data = json.loads(field_map_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"field_map JSON inválido: {exc}"
            ) from exc
        return SupplierFieldMap(
            field_map=dict(data.get("field_map", data)),
            unit_aliases=dict(data.get("unit_aliases", {})),
            default_currency=data.get("default_currency"),
        )
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Aporta field_mapping_id o field_map_json",
    )


@router.post("/sample-import", dependencies=[Depends(verify_csrf)])
def sample_import(
    admin: AdminUser,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    retailer_id: Annotated[uuid.UUID, Form()],
    field_mapping_id: Annotated[uuid.UUID | None, Form()] = None,
    field_map_json: Annotated[str | None, Form()] = None,
    fmt: Annotated[str | None, Form()] = None,
    dry_run: Annotated[bool, Form()] = True,
) -> dict[str, Any]:
    """Upload a licensed sample and run the 10-step import pipeline (dry-run by default)."""
    retailer = _get_retailer(db, retailer_id)
    field_map = _resolve_field_map(db, field_mapping_id, field_map_json)
    content = file.file.read()

    resolved_fmt = (fmt or ("json" if (file.filename or "").endswith(".json") else "csv")).lower()
    importer = (
        JsonLicensedCatalogImporter()
        if resolved_fmt == "json"
        else CsvLicensedCatalogImporter()
    )
    report = run_sample_import(
        db, retailer, content, field_map, importer, dry_run=dry_run
    )
    return report.as_dict()


# --------------------------------------------------------------------------- #
# Review queue
# --------------------------------------------------------------------------- #
def _candidate_row(
    m: IngredientProductMapping, ingredient: Ingredient, variant: ProductVariant | None
) -> dict[str, Any]:
    return {
        "id": str(m.public_id),
        "canonical_name": ingredient.canonical_name,
        "ingredient_display": ingredient.display_name,
        "product_variant_id": str(variant.public_id) if variant is not None else None,
        "product_name": variant.display_name if variant is not None else None,
        "confidence_score": str(m.confidence_score) if m.confidence_score is not None else None,
        "match_method": m.match_method,
        "verification_status": m.verification_status,
    }


@router.get("/review-queue")
def review_queue(
    admin: AdminUser, db: DbSession, retailer_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    """Pending machine candidates awaiting human sign-off (inactive, machine_verified)."""
    stmt = (
        select(IngredientProductMapping, Ingredient, ProductVariant)
        .join(Ingredient, Ingredient.id == IngredientProductMapping.ingredient_id)
        .join(
            ProductVariant,
            ProductVariant.id == IngredientProductMapping.product_variant_id,
            isouter=True,
        )
        .where(
            IngredientProductMapping.verification_status == "machine_verified",
            IngredientProductMapping.is_active.is_(False),
        )
        .order_by(IngredientProductMapping.confidence_score.desc())
    )
    if retailer_id is not None:
        retailer = _get_retailer(db, retailer_id)
        stmt = stmt.where(IngredientProductMapping.retailer_id == retailer.id)
    return [_candidate_row(m, ing, v) for m, ing, v in db.execute(stmt).all()]


def _get_candidate(db: DbSession, mapping_id: uuid.UUID) -> IngredientProductMapping:
    mapping = db.execute(
        select(IngredientProductMapping).where(
            IngredientProductMapping.public_id == mapping_id
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Candidato no encontrado")
    return mapping


@router.post("/review/{mapping_id}/approve", dependencies=[Depends(verify_csrf)])
def approve_candidate(
    mapping_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Approve a candidate: human-verified and active (usable by the planner)."""
    mapping = _get_candidate(db, mapping_id)
    mapping.verification_status = "human_verified"
    mapping.is_active = True
    mapping.verified_by = admin.id
    mapping.verified_at = _now()
    db.flush()
    return {"id": str(mapping.public_id), "verification_status": mapping.verification_status,
            "is_active": mapping.is_active}


@router.post("/review/{mapping_id}/reject", dependencies=[Depends(verify_csrf)])
def reject_candidate(
    mapping_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Reject a candidate: disputed and inactive (never used by the planner)."""
    mapping = _get_candidate(db, mapping_id)
    mapping.verification_status = "disputed"
    mapping.is_active = False
    mapping.verified_by = admin.id
    mapping.verified_at = _now()
    db.flush()
    return {"id": str(mapping.public_id), "verification_status": mapping.verification_status,
            "is_active": mapping.is_active}


# --------------------------------------------------------------------------- #
# Coverage (FASE 5 gate input)
# --------------------------------------------------------------------------- #
@router.get("/coverage")
def coverage(admin: AdminUser, db: DbSession) -> list[dict[str, Any]]:
    """Per-retailer costable-variant and verified-ingredient-coverage metrics."""
    total_ingredients = db.scalar(select(func.count(Ingredient.id))) or 0
    retailers = db.execute(select(Retailer).order_by(Retailer.name)).scalars().all()
    out: list[dict[str, Any]] = []
    for r in retailers:
        costable_variants = db.scalar(
            select(func.count(ProductVariant.id)).where(
                ProductVariant.retailer_id == r.id,
                func.lower(ProductVariant.net_content_unit).in_(COSTABLE_UNITS),
            )
        ) or 0
        verified_ingredients = db.scalar(
            select(func.count(func.distinct(IngredientProductMapping.ingredient_id))).where(
                IngredientProductMapping.retailer_id == r.id,
                IngredientProductMapping.is_active.is_(True),
                IngredientProductMapping.verification_status == "human_verified",
            )
        ) or 0
        pending = db.scalar(
            select(func.count(IngredientProductMapping.id)).where(
                IngredientProductMapping.retailer_id == r.id,
                IngredientProductMapping.verification_status == "machine_verified",
                IngredientProductMapping.is_active.is_(False),
            )
        ) or 0
        out.append(
            {
                "retailer_id": str(r.public_id),
                "retailer": r.name,
                "costable_variants": costable_variants,
                "verified_ingredients": verified_ingredients,
                "ingredients_total": total_ingredients,
                "ingredient_coverage_ratio": (
                    round(verified_ingredients / total_ingredients, 4)
                    if total_ingredients
                    else 0.0
                ),
                "pending_candidates": pending,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Readiness gate (FASE 5)
# --------------------------------------------------------------------------- #
@router.get("/readiness/{retailer_id}")
def readiness(
    retailer_id: uuid.UUID,
    admin: AdminUser,
    db: DbSession,
    min_coverage: float = 0.60,
    license_verified: bool = False,
) -> dict[str, Any]:
    """Evaluate the 8 exit criteria for a chain. ``can_retire_demo`` is True only if all pass.

    ``license_verified`` is the operator's attestation that the licence is signed (a real
    contract cannot be checked programmatically); ``min_coverage`` is the agreed floor.
    """
    retailer = _get_retailer(db, retailer_id)
    report = evaluate_readiness(
        db,
        retailer,
        GateConfig(min_ingredient_coverage=min_coverage, license_verified=license_verified),
    )
    return report.as_dict()
