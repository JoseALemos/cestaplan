"""Admin: audited staging → production promotion for a price provider (phase 2, spec §P/§O).

Platform-admin only; mutations require CSRF. These routes are the explicit, human-driven bridge
that turns approved staging data into productive prices — the ONLY sanctioned path now that the
legacy direct-write sync is blocked. A blocked gate returns 409 with typed reasons, never a write.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from cestaplan_api.deps import AdminUser, DbSession, verify_csrf
from cestaplan_api.services import provider_promotion as promo

router = APIRouter(prefix="/api/v1/admin/providers", tags=["admin"])


@router.get("/{provider_code}/promotion-status")
def promotion_status(provider_code: str, admin: AdminUser, db: DbSession) -> dict[str, Any]:
    """Gate reasons + what an approval/promotion would touch. Read-only."""
    return promo.promotion_status(db, provider_code=provider_code)


@router.post("/{provider_code}/production-approval", dependencies=[Depends(verify_csrf)])
def approve_production(
    provider_code: str, admin: AdminUser, db: DbSession
) -> dict[str, Any]:
    """Grant production approval (records actor + timestamp). 409 if prerequisites are not met."""
    try:
        activation = promo.approve_provider_production(
            db, provider_code=provider_code, actor_id=admin.id
        )
    except promo.PromotionBlocked as exc:
        raise HTTPException(status_code=409, detail={"reasons": exc.reasons}) from exc
    db.commit()
    return {
        "provider_code": provider_code,
        "production_enabled": activation.production_enabled,
        "production_approved": activation.production_approved,
        "production_approved_at": (
            activation.production_approved_at.isoformat()
            if activation.production_approved_at
            else None
        ),
        "production_approved_by": activation.production_approved_by,
        "activation_state": activation.activation_state,
    }


@router.post("/{provider_code}/promote", dependencies=[Depends(verify_csrf)])
def promote(
    provider_code: str,
    admin: AdminUser,
    db: DbSession,
    dry_run: bool = Query(default=False),
) -> dict[str, Any]:
    """Materialize productive mappings + prices from approved staging data. 409 if the gate is not
    clear. ``dry_run=true`` computes exact counts and writes nothing (the transaction is rolled
    back)."""
    try:
        result = promo.promote_provider_to_production(
            db, provider_code=provider_code, actor_id=admin.id, dry_run=dry_run
        )
    except promo.PromotionBlocked as exc:
        raise HTTPException(status_code=409, detail={"reasons": exc.reasons}) from exc
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return result.as_dict()
