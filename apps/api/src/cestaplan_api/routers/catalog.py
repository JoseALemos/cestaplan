"""Catalog router (prefix ``/api/v1``): read-only retailers, stores and recipe detail.

Every route requires an authenticated session. Retailers and stores are addressed by
their public UUID. Recipe detail enforces the household boundary (no IDOR): a caller may
read public/synthetic recipes and recipes that belong to a household they are a member of,
but never another household's private recipe. Money and quantities are returned as strings.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from cestaplan_api.deps import CurrentUser, DbSession
from cestaplan_api.models import (
    HouseholdMember,
    Ingredient,
    Product,
    ProductBarcode,
    ProductPrice,
    Recipe,
    Retailer,
    Store,
)
from cestaplan_api.services.open_prices_sync import ensure_open_prices_data_source

router = APIRouter(prefix="/api/v1", tags=["catalog"])


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Retailers / stores
# --------------------------------------------------------------------------- #
@router.get("/retailers")
def list_retailers(user: CurrentUser, db: DbSession) -> list[dict[str, Any]]:
    """List active retailers that currently have at least one priced product.

    Retailers with no ``ProductPrice`` (e.g. Deza, or chains whose stores are seeded but
    not yet synced) are hidden until they have real prices. The synthetic demo retailer
    (MercaEjemplo) has prices and stays visible.
    """
    priced_retailer_ids = select(ProductPrice.retailer_id).distinct().scalar_subquery()
    retailers = db.execute(
        select(Retailer)
        .where(Retailer.is_active.is_(True), Retailer.id.in_(priced_retailer_ids))
        .order_by(Retailer.name)
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

    # Only stores that currently have at least one priced product, with a per-store count.
    count_rows = db.execute(
        select(
            ProductPrice.store_id,
            func.count(func.distinct(ProductPrice.product_id)),
        )
        .where(ProductPrice.retailer_id == retailer.id)
        .group_by(ProductPrice.store_id)
    ).all()
    price_counts: dict[int, int] = {row[0]: row[1] for row in count_rows}
    stores = db.execute(
        select(Store)
        .where(
            Store.retailer_id == retailer.id,
            Store.is_active.is_(True),
            Store.id.in_(price_counts.keys()),
        )
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
            "priced_product_count": price_counts.get(s.id, 0),
        }
        for s in stores
    ]


@router.get("/retailers/{retailer_id}/stores/{store_id}/prices")
def list_store_prices(
    retailer_id: uuid.UUID,
    store_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    search: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Real Open Prices observations for one store — the "Precios reales" viewer.

    For each product priced at this store, returns only the *latest* observation
    (append-only history is never collapsed elsewhere). Restricted to real, community
    data (``source_type='open_dataset'``, ``is_synthetic=False``) — this never reflects
    the synthetic demo catalogue and never feeds the planner. IDOR-safe: the store must
    belong to the given retailer, both addressed by public UUID. A store with zero real
    prices (e.g. seeded but not yet synced) is a valid 200 with an empty ``items`` list —
    it is simply hidden from the store picker (see :func:`list_stores`), not an error.
    """
    retailer = db.execute(
        select(Retailer).where(Retailer.public_id == retailer_id)
    ).scalar_one_or_none()
    if retailer is None or not retailer.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Distribuidor no encontrado")

    store = db.execute(
        select(Store).where(Store.public_id == store_id, Store.retailer_id == retailer.id)
    ).scalar_one_or_none()
    if store is None or not store.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")

    # One row per product: DISTINCT ON keeps only the latest observation (ties broken by
    # the highest id, i.e. the most recently inserted row).
    query = (
        select(ProductPrice, Product)
        .distinct(ProductPrice.product_id)
        .join(Product, Product.id == ProductPrice.product_id)
        .where(
            ProductPrice.store_id == store.id,
            ProductPrice.source_type == "open_dataset",
            ProductPrice.is_synthetic.is_(False),
        )
    )
    search = (search or "").strip()
    if search:
        query = query.where(Product.name.ilike(f"%{search}%"))
    query = query.order_by(
        ProductPrice.product_id, ProductPrice.observed_at.desc(), ProductPrice.id.desc()
    )
    rows = list(db.execute(query).all())

    # Paginate in Python: per-store real-price counts are sparse (tens of rows), and the
    # DISTINCT ON above already collapsed history, so this stays cheap.
    rows.sort(key=lambda row: (row[1].name or "", row[1].id))
    total = len(rows)
    start = (page - 1) * size
    page_rows = rows[start : start + size]

    product_ids = [product.id for _, product in page_rows]
    primary_barcode: dict[int, str] = {}
    if product_ids:
        for product_id, barcode in db.execute(
            select(ProductBarcode.product_id, ProductBarcode.barcode)
            .where(ProductBarcode.product_id.in_(product_ids))
            .order_by(
                ProductBarcode.product_id,
                ProductBarcode.is_primary.desc(),
                ProductBarcode.id,
            )
        ).all():
            primary_barcode.setdefault(product_id, barcode)

    data_source = ensure_open_prices_data_source(db)

    items = [
        {
            "product_id": str(product.public_id),
            "product_name": product.name,
            "brand": product.brand,
            "barcode": primary_barcode.get(product.id),
            "amount": _s(price.amount),
            "currency": price.currency,
            "unit_price": _s(price.unit_price),
            "package_quantity": _s(price.package_quantity),
            "package_unit": price.package_unit,
            "observed_at": price.observed_at.date().isoformat(),
            "source_type": price.source_type,
            "source_name": price.source_name,
            "source_url": price.source_url,
            "is_synthetic": price.is_synthetic,
        }
        for price, product in page_rows
    ]

    return {
        "store": {
            "id": str(store.public_id),
            "name": store.name,
            "locality": store.locality,
            "postal_code": store.postal_code,
            "catalog_updated_at": store.catalog_updated_at,
        },
        "page": page,
        "size": size,
        "count": total,
        "items": items,
        "attribution": data_source.attribution_text,
        "license_code": data_source.license_code,
    }


# --------------------------------------------------------------------------- #
# Ingredients (canonical list, for pantry autocomplete)
# --------------------------------------------------------------------------- #
@router.get("/ingredients")
def list_ingredients(
    user: CurrentUser,
    db: DbSession,
    search: str | None = None,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Canonical ingredients for the pantry autocomplete.

    Optional case-insensitive ``search`` matches the canonical or display name. Returns the
    same catalogue the planner and pantry resolution use, so any offered item can be stocked.
    """
    query = select(Ingredient)
    search = (search or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.where(
            Ingredient.display_name.ilike(pattern)
            | Ingredient.canonical_name.ilike(pattern)
        )
    rows = db.execute(
        query.order_by(Ingredient.display_name).limit(limit)
    ).scalars().all()
    return [
        {
            "canonical_name": ing.canonical_name,
            "display_name": ing.display_name,
            "default_unit": ing.default_unit,
            "category_code": ing.category_code,
        }
        for ing in rows
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
