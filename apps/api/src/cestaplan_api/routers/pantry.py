"""Pantry (despensa) router: household stock CRUD.

Wired in ``main.py`` as ``app.include_router(pantry.router)``.

    from cestaplan_api.routers import pantry
    app.include_router(pantry.router)

Prefix: ``/api/v1/households/{household_id}/pantry``.

Authorization is by household membership and role, verified on the server for every
route (no IDOR): the household is addressed by public UUID and resolved through
:func:`cestaplan_api.deps.get_household_context`; items are addressed by their own public
UUID and always constrained to the resolved household. Roles per docs/SECURITY.md §3.1 —
owner/editor may mutate stock, viewer is read-only. Mutations require CSRF.

Pantry stock reduces what a plan must buy: the deterministic engine's ``PantryCalculator``
subtracts these rows from the shopping list (see ``services.planning_context``), so keeping
the pantry current makes the next plan cheaper. This router never touches generation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from cestaplan_api.deps import (
    CurrentUser,
    DbSession,
    HouseholdCtx,
    HouseholdCtxEditor,
    verify_csrf,
)
from cestaplan_api.models import Ingredient, PantryItem
from cestaplan_api.schemas.pantry import (
    PantryItemCreate,
    PantryItemResponse,
    PantryItemUpdate,
)
from cestaplan_api.services.audit import record_audit

router = APIRouter(prefix="/api/v1/households/{household_id}/pantry", tags=["pantry"])


def _to_expiry(value: date | None) -> datetime | None:
    """Store a caducidad date as a timezone-aware UTC datetime (the column type)."""
    return datetime(value.year, value.month, value.day, tzinfo=UTC) if value else None


def _resolve_ingredient(db: DbSession, name: str) -> Ingredient:
    """Map a canonical name or free text to a known ingredient (case-insensitive).

    An unresolved item is rejected: there is no free-text label column on ``pantry_item``
    and an item without an ``ingredient_id`` is ignored by the planner, so storing one
    would be a silent no-op. 422 tells the caller to pick a known ingredient.
    """
    needle = name.strip().lower()
    ingredient = db.execute(
        select(Ingredient)
        .where(func.lower(Ingredient.canonical_name) == needle)
        .limit(1)
    ).scalar_one_or_none()
    if ingredient is None:
        ingredient = db.execute(
            select(Ingredient)
            .where(func.lower(Ingredient.display_name) == needle)
            .order_by(Ingredient.id)
            .limit(1)
        ).scalar_one_or_none()
    if ingredient is None:
        raise HTTPException(
            status_code=422,
            detail=f"Ingrediente no reconocido: {name!r}",
        )
    return ingredient


def _get_item(db: DbSession, household_id: int, item_id: uuid.UUID) -> PantryItem:
    item = db.execute(
        select(PantryItem).where(
            PantryItem.public_id == item_id,
            PantryItem.household_id == household_id,
            PantryItem.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Artículo de despensa no encontrado"
        )
    return item


@router.get("", response_model=list[PantryItemResponse])
def list_pantry(ctx: HouseholdCtx, db: DbSession) -> list[PantryItemResponse]:
    """List the household's (non-deleted) pantry items with their canonical ingredient."""
    rows = db.execute(
        select(PantryItem, Ingredient)
        .join(Ingredient, Ingredient.id == PantryItem.ingredient_id)
        .where(
            PantryItem.household_id == ctx.household.id,
            PantryItem.deleted_at.is_(None),
        )
        .order_by(Ingredient.display_name)
    ).all()
    return [PantryItemResponse.from_model(item, ingredient) for item, ingredient in rows]


@router.post(
    "",
    response_model=PantryItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def add_pantry_item(
    payload: PantryItemCreate,
    ctx: HouseholdCtxEditor,
    user: CurrentUser,
    db: DbSession,
) -> PantryItemResponse:
    """Add stock to the household pantry (editor+). Resolves the ingredient server-side."""
    ingredient = _resolve_ingredient(db, payload.name)
    item = PantryItem(
        household_id=ctx.household.id,
        ingredient_id=ingredient.id,
        quantity=payload.quantity,
        unit=payload.unit,
        expires_at=_to_expiry(payload.expires_at),
    )
    db.add(item)
    db.flush()
    db.refresh(item)  # reflect the column's numeric(12,4) scale in the response
    record_audit(db, action="household.pantry.add", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="pantry_item",
                 entity_public_id=item.public_id)
    return PantryItemResponse.from_model(item, ingredient)


@router.patch(
    "/{item_id}",
    response_model=PantryItemResponse,
    dependencies=[Depends(verify_csrf)],
)
def update_pantry_item(
    item_id: uuid.UUID,
    payload: PantryItemUpdate,
    ctx: HouseholdCtxEditor,
    user: CurrentUser,
    db: DbSession,
) -> PantryItemResponse:
    """Update an item's quantity/unit/caducidad (editor+). Only provided fields change."""
    item = _get_item(db, ctx.household.id, item_id)
    fields = payload.model_dump(exclude_unset=True)
    if "quantity" in fields:
        item.quantity = payload.quantity  # type: ignore[assignment]
    if "unit" in fields:
        item.unit = payload.unit  # type: ignore[assignment]
    if "expires_at" in fields:
        item.expires_at = _to_expiry(payload.expires_at)
    db.flush()
    db.refresh(item)  # reflect the column's numeric(12,4) scale in the response
    record_audit(db, action="household.pantry.update", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="pantry_item",
                 entity_public_id=item.public_id)
    ingredient = db.get(Ingredient, item.ingredient_id)
    assert ingredient is not None  # ingredient_id is always set on creation
    return PantryItemResponse.from_model(item, ingredient)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(verify_csrf)])
def delete_pantry_item(
    item_id: uuid.UUID,
    ctx: HouseholdCtxEditor,
    user: CurrentUser,
    db: DbSession,
) -> None:
    """Soft-delete a pantry item (editor+); it stops reducing future plans immediately."""
    item = _get_item(db, ctx.household.id, item_id)
    item.deleted_at = datetime.now(UTC)
    db.flush()
    record_audit(db, action="household.pantry.delete", actor_user_id=user.id,
                 household_id=ctx.household.id, entity_type="pantry_item",
                 entity_public_id=item.public_id)
