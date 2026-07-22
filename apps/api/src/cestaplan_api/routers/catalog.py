"""Catalog router (prefix ``/api/v1``): read-only retailers, stores and recipe detail.

Every route requires an authenticated session. Retailers and stores are addressed by
their public UUID. Recipe detail enforces the household boundary (no IDOR): a caller may
read public/synthetic recipes and recipes that belong to a household they are a member of,
but never another household's private recipe. Money and quantities are returned as strings.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from cestaplan_api.deps import CurrentUser, DbSession
from cestaplan_api.models import (
    HouseholdMember,
    Ingredient,
    Recipe,
    Retailer,
    Store,
)

router = APIRouter(prefix="/api/v1", tags=["catalog"])


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Retailers / stores
# --------------------------------------------------------------------------- #
@router.get("/retailers")
def list_retailers(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    """List active retailers (public id, name, synthetic flag)."""
    retailers = db.execute(
        select(Retailer).where(Retailer.is_active.is_(True)).order_by(Retailer.name)
    ).scalars().all()
    return [
        {
            "id": str(r.public_id),
            "name": r.name,
            "is_synthetic": r.is_synthetic,
        }
        for r in retailers
    ]


@router.get("/retailers/{retailer_id}/stores")
def list_stores(
    retailer_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> list[dict[str, Any]]:
    """List a retailer's active stores with location and price-coverage metadata."""
    retailer = db.execute(
        select(Retailer).where(Retailer.public_id == retailer_id)
    ).scalar_one_or_none()
    if retailer is None or not retailer.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Distribuidor no encontrado")

    stores = db.execute(
        select(Store)
        .where(Store.retailer_id == retailer.id, Store.is_active.is_(True))
        .order_by(Store.name)
    ).scalars().all()
    return [
        {
            "id": str(s.public_id),
            "name": s.name,
            "province": s.province,
            "locality": s.locality,
            "postal_code": s.postal_code,
            "external_store_id": s.external_code,
            "catalog_updated_at": s.catalog_updated_at,
            "price_coverage": _s(s.price_coverage_hint),
        }
        for s in stores
    ]


# --------------------------------------------------------------------------- #
# Recipe detail
# --------------------------------------------------------------------------- #
@router.get("/recipes/{recipe_id}")
def get_recipe(
    recipe_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Full recipe detail. Public/synthetic recipes are readable by anyone; a private
    recipe is readable only by a member of its household (404 otherwise, no disclosure)."""
    recipe = db.execute(
        select(Recipe).where(Recipe.public_id == recipe_id)
    ).scalar_one_or_none()
    if recipe is None or recipe.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Receta no encontrada")

    if not _may_read_recipe(db, recipe, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Receta no encontrada")

    ingredient_ids = [ri.ingredient_id for ri in recipe.ingredients]
    allergens: set[str] = set()
    if ingredient_ids:
        for codes in db.execute(
            select(Ingredient.allergen_codes).where(Ingredient.id.in_(set(ingredient_ids)))
        ).scalars().all():
            allergens |= set(codes or [])

    return {
        "id": str(recipe.public_id),
        "title": recipe.title,
        "description": recipe.description,
        "servings": recipe.servings,
        "meal_types": list(recipe.meal_types or []),
        "cuisine": recipe.cuisine,
        "preference_tags": list(recipe.preference_tags or []),
        "preparation_minutes": recipe.preparation_minutes,
        "cooking_minutes": recipe.cooking_minutes,
        "required_equipment": list(recipe.required_equipment or []),
        "ingredients": [
            {
                "canonical_name": ri.canonical_name,
                "display_name": ri.display_name or ri.canonical_name,
                "quantity": _s(ri.quantity),
                "unit": ri.unit,
                "optional": ri.optional,
                "substitution_group": ri.substitution_group,
            }
            for ri in recipe.ingredients
        ],
        "steps": [
            {"position": s.step_number, "instruction": s.instruction}
            for s in sorted(recipe.steps, key=lambda s: s.step_number)
        ],
        "allergens": sorted(allergens),
        "nutrition": None,
    }


def _may_read_recipe(db: DbSession, recipe: Recipe, user_id: int) -> bool:
    if recipe.is_public or recipe.is_synthetic:
        return True
    if recipe.household_id is None:
        return False
    member = db.execute(
        select(HouseholdMember.id).where(
            HouseholdMember.household_id == recipe.household_id,
            HouseholdMember.user_id == user_id,
        )
    ).scalar_one_or_none()
    return member is not None
