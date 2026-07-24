"""Internal admin review queue for provider ingredient mappings (spec §3/§4/§5/§10).

Every route requires a platform admin (never exposed to end users); mutations require CSRF.
External data stays in review and is NEVER used in production; nothing here changes
production_eligibility.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from cestaplan_api.deps import AdminUser, DbSession, verify_csrf
from cestaplan_api.models import (
    PriceObservation,
    ProductVariant,
    ProviderIngredientMapping,
)
from cestaplan_api.services import mapping_review as mr

router = APIRouter(prefix="/api/v1/admin/ingredient-product-mappings", tags=["admin"])

_REVIEW_NOTICE = "Los datos externos están en revisión y no se utilizan en producción."


class DecisionBody(BaseModel):
    reason: str | None = None


class RejectBody(BaseModel):
    reason: str


class BulkBody(BaseModel):
    mapping_ids: list[int]
    reason: str | None = None


def _variant_facts(db: DbSession, row: ProviderIngredientMapping) -> dict[str, Any]:
    if row.normalized_product_id is None:
        return {}
    var = (
        db.execute(
            select(ProductVariant).where(ProductVariant.product_id == row.normalized_product_id)
        )
        .scalars()
        .first()
    )
    if var is None:
        return {}
    price = (
        db.execute(
            select(PriceObservation.amount)
            .where(
                PriceObservation.product_variant_id == var.id,
                PriceObservation.staging_only.is_(True),
            )
            .order_by(PriceObservation.observed_at.desc())
        )
        .scalars()
        .first()
    )
    return {
        "net_content": (
            f"{var.net_content_quantity}{var.net_content_unit}"
            if var.net_content_quantity is not None
            else None
        ),
        "sell_unit": var.sell_unit,
        "variable_weight": var.variable_weight,
        "unit_price": None if var.unit_price is None else str(var.unit_price),
        "unit_price_unit": var.unit_price_unit,
        "price": None if price is None else str(price),
    }


def _serialize(db: DbSession, row: ProviderIngredientMapping, unlock: int) -> dict[str, Any]:
    ev = row.evidence_json or {}
    return {
        "mapping_id": row.id,
        "canonical_ingredient_key": row.canonical_ingredient_key,
        "ingredient_id": row.ingredient_id,
        "provider_code": row.provider_code,
        "retailer_slug": row.retailer_slug,
        "external_product_id": row.external_product_id,
        "original_product_name": ev.get("product_name"),
        "matched_rules": ev.get("matched_rules", []),
        "failed_rules": ev.get("failed_rules", []),
        "warnings": ev.get("warnings", []),
        "exclusion_warning": any("excluding" in str(w) for w in ev.get("warnings", [])),
        "lexical_score": None if row.lexical_score is None else str(row.lexical_score),
        "semantic_score": None if row.semantic_score is None else str(row.semantic_score),
        "category_score": None if row.category_score is None else str(row.category_score),
        "confidence_score": str(row.confidence_score),
        "mapping_status": row.mapping_status,
        "mapping_method": row.mapping_method,
        "mapping_version": row.mapping_version,
        "unit_compatibility": row.unit_compatibility,
        "preparation_compatibility": row.preparation_compatibility,
        "dietary_compatibility": row.dietary_compatibility,
        "allergen_compatibility": row.allergen_compatibility,
        "required_review": row.required_review,
        "active": row.active,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "reviewed_by": row.reviewed_by,
        "recipes_potentially_unlocked": unlock,
        **_variant_facts(db, row),
        "review_notice": _REVIEW_NOTICE,
    }


@router.get("/candidates")
def list_candidates(
    admin: AdminUser,
    db: DbSession,
    provider_code: str | None = None,
    retailer_slug: str | None = None,
    ingredient_id: int | None = None,
    canonical_ingredient_key: str | None = None,
    mapping_status: str | None = None,
    required_review: bool | None = None,
    minimum_confidence: float | None = None,
    maximum_confidence: float | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List review candidates, ordered by potential recipe-unlock impact, then confidence."""
    stmt = mr._filtered(
        select(ProviderIngredientMapping),
        provider_code=provider_code,
        retailer_slug=retailer_slug,
        ingredient_id=ingredient_id,
        canonical_ingredient_key=canonical_ingredient_key,
        mapping_status=mapping_status,
        required_review=required_review,
        minimum_confidence=None if minimum_confidence is None else Decimal(str(minimum_confidence)),
        maximum_confidence=None if maximum_confidence is None else Decimal(str(maximum_confidence)),
    )
    rows = list(db.execute(stmt).scalars())  # type: ignore[arg-type]
    now = datetime.now(UTC)
    unlock_by_provider: dict[str, dict[int, int]] = {}
    for prov in {r.provider_code for r in rows}:
        unlock_by_provider[prov] = mr._impact(db, prov, now).unlock_map

    def _u(r: ProviderIngredientMapping) -> int:
        return unlock_by_provider.get(r.provider_code, {}).get(r.ingredient_id, 0)

    rows.sort(key=lambda r: (_u(r), float(r.confidence_score or 0), -r.id), reverse=True)
    page = rows[offset : offset + limit]
    return {
        "total": len(rows),
        "review_notice": _REVIEW_NOTICE,
        "items": [_serialize(db, r, _u(r)) for r in page],
    }


@router.get("/{mapping_id}")
def get_candidate(mapping_id: int, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    row = db.get(ProviderIngredientMapping, mapping_id)
    if row is None:
        raise HTTPException(status_code=404, detail="mapping not found")
    unlock = mr.recipes_potentially_unlocked(db, row.provider_code, row.ingredient_id)
    return _serialize(db, row, unlock)


@router.post("/{mapping_id}/approve", dependencies=[Depends(verify_csrf)])
def approve(mapping_id: int, body: DecisionBody, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    try:
        row = mr.approve(db, mapping_id, reviewer_id=admin.id, reason=body.reason)
    except mr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"mapping_id": row.id, "mapping_status": row.mapping_status, "active": row.active}


@router.post("/{mapping_id}/reject", dependencies=[Depends(verify_csrf)])
def reject(mapping_id: int, body: RejectBody, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    try:
        row = mr.reject(db, mapping_id, reviewer_id=admin.id, reason=body.reason)
    except mr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"mapping_id": row.id, "mapping_status": row.mapping_status, "active": row.active}


@router.post("/{mapping_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke(mapping_id: int, body: RejectBody, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    try:
        row = mr.revoke(db, mapping_id, reviewer_id=admin.id, reason=body.reason)
    except mr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"mapping_id": row.id, "mapping_status": row.mapping_status, "active": row.active}


@router.post("/bulk-approve", dependencies=[Depends(verify_csrf)])
def bulk_approve(body: BulkBody, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    try:
        result = mr.bulk_approve(db, body.mapping_ids, reviewer_id=admin.id, reason=body.reason)
    except mr.ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/bulk-reject", dependencies=[Depends(verify_csrf)])
def bulk_reject(body: BulkBody, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    if not body.reason:
        raise HTTPException(status_code=400, detail="reason is required")
    result = mr.bulk_reject(db, body.mapping_ids, reviewer_id=admin.id, reason=body.reason)
    db.commit()
    return result


__all__ = ["router"]
