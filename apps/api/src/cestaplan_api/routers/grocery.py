"""Grocery-list router (prefix ``/api/v1/plans``): read, check off, add, substitute.

The consolidated list is materialized by the worker when a plan completes. The
frontend owns offline state (IndexedDB); the backend just persists the bought/
unbought flag and manual edits. Membership is verified on every route (no IDOR).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from cestaplan_api.deps import CurrentUser, DbSession, verify_csrf
from cestaplan_api.models import (
    GroceryList,
    GroceryListItem,
    Ingredient,
    Product,
    ProductPrice,
)
from cestaplan_api.schemas.plan import GroceryItemIn, SubstituteRequest
from cestaplan_api.services.plan_service import resolve_plan, serialize_grocery_list

router = APIRouter(prefix="/api/v1/plans", tags=["grocery"])


def _resolve_list(db: DbSession, meal_plan) -> GroceryList:
    grocery = db.execute(
        select(GroceryList).where(GroceryList.meal_plan_id == meal_plan.id)
    ).scalar_one_or_none()
    if grocery is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lista de compra no encontrada")
    return grocery


def _resolve_item(db: DbSession, grocery: GroceryList, item_id: uuid.UUID) -> GroceryListItem:
    item = db.execute(
        select(GroceryListItem).where(
            GroceryListItem.public_id == item_id,
            GroceryListItem.grocery_list_id == grocery.id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Artículo no encontrado")
    return item


def _latest_price(db: DbSession, product_id: int) -> ProductPrice | None:
    return db.execute(
        select(ProductPrice)
        .where(ProductPrice.product_id == product_id)
        .order_by(ProductPrice.observed_at.desc(), ProductPrice.id.desc())
    ).scalars().first()


def _latest_price_for_retailer(
    db: DbSession, product_id: int, retailer_id: int | None
) -> ProductPrice | None:
    """Latest price for a product WITHIN one chain — prices are never mixed across chains."""
    if retailer_id is None:
        return None
    return db.execute(
        select(ProductPrice)
        .where(
            ProductPrice.product_id == product_id,
            ProductPrice.retailer_id == retailer_id,
        )
        .order_by(ProductPrice.observed_at.desc(), ProductPrice.id.desc())
    ).scalars().first()


@router.get("/{meal_plan_id}/grocery-list")
def get_grocery_list(
    meal_plan_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict:
    """Consolidated grocery list grouped by category."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id)
    return serialize_grocery_list(db, meal_plan)


@router.get("/{meal_plan_id}/grocery-list/product-search")
def search_products(
    meal_plan_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    search: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=40),
) -> dict:
    """Search the plan's chain catalogue for a product to substitute into a line.

    Scoped to the plan's retailer (prices are never mixed across chains), so every product
    returned has a price that will actually re-cost the line — unlike the community
    "Precios reales" (Open Prices) viewer, which is a separate source the planner never
    uses. Returns nothing for a blank query (never dumps the whole catalogue).
    """
    meal_plan = resolve_plan(db, user.id, meal_plan_id)
    term = search.strip()
    if not term or meal_plan.retailer_id is None:
        return {"items": [], "count": 0}

    # Latest price per product within the plan's chain, matched by product name.
    rows = list(
        db.execute(
            select(ProductPrice, Product)
            .distinct(ProductPrice.product_id)
            .join(Product, Product.id == ProductPrice.product_id)
            .where(
                ProductPrice.retailer_id == meal_plan.retailer_id,
                Product.deleted_at.is_(None),
                Product.name.ilike(f"%{term}%"),
            )
            .order_by(
                ProductPrice.product_id,
                ProductPrice.observed_at.desc(),
                ProductPrice.id.desc(),
            )
        ).all()
    )
    rows.sort(key=lambda row: (row[1].name or "", row[1].id))
    rows = rows[:limit]
    items = [
        {
            "product_id": str(product.public_id),
            "product_name": product.name,
            "brand": product.brand,
            "package_quantity": (
                str(price.package_quantity) if price.package_quantity is not None else None
            ),
            "package_unit": price.package_unit,
            "amount": str(price.amount) if price.amount is not None else None,
            "currency": price.currency,
        }
        for price, product in rows
    ]
    return {"items": items, "count": len(items)}


@router.post(
    "/{meal_plan_id}/grocery-list/items/{item_id}/toggle",
    dependencies=[Depends(verify_csrf)],
)
def toggle_item(
    meal_plan_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Mark an item bought/unbought (persists offline check state)."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    grocery = _resolve_list(db, meal_plan)
    item = _resolve_item(db, grocery, item_id)
    item.is_checked = not item.is_checked
    db.flush()
    return {"id": str(item.public_id), "is_checked": item.is_checked}


@router.post(
    "/{meal_plan_id}/grocery-list/items",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def add_item(
    meal_plan_id: uuid.UUID,
    payload: GroceryItemIn,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Add a manual grocery item to the list."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    grocery = _resolve_list(db, meal_plan)

    ingredient_id: int | None = None
    if payload.ingredient_id is not None:
        ingredient = db.execute(
            select(Ingredient).where(Ingredient.public_id == payload.ingredient_id)
        ).scalar_one_or_none()
        if ingredient is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Ingrediente no encontrado")
        ingredient_id = ingredient.id

    product_id: int | None = None
    unit_price: Decimal | None = None
    price_id: int | None = None
    price_status = "missing"
    if payload.product_id is not None:
        product = db.execute(
            select(Product).where(Product.public_id == payload.product_id)
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
        product_id = product.id
        price = _latest_price(db, product.id)
        if price is not None:
            unit_price = price.unit_price
            price_id = price.id
            price_status = "known"

    item = GroceryListItem(
        grocery_list_id=grocery.id,
        product_id=product_id,
        ingredient_id=ingredient_id,
        needed_quantity=payload.needed_quantity,
        pantry_quantity=Decimal("0"),
        pending_quantity=payload.needed_quantity,
        package_unit=payload.unit,
        unit_price=unit_price,
        price_product_price_id=price_id,
        price_status=price_status,
        is_checked=False,
    )
    db.add(item)
    db.flush()
    return {"id": str(item.public_id)}


@router.post(
    "/{meal_plan_id}/grocery-list/items/{item_id}/substitute",
    dependencies=[Depends(verify_csrf)],
)
def substitute_item(
    meal_plan_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: SubstituteRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """Substitute the concrete product on a grocery item with another product."""
    meal_plan = resolve_plan(db, user.id, meal_plan_id, require_edit=True)
    grocery = _resolve_list(db, meal_plan)
    item = _resolve_item(db, grocery, item_id)

    product = db.execute(
        select(Product).where(Product.public_id == payload.product_id)
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")

    # Price from the plan's chain only (never mix chains). The picker only offers
    # products priced for this chain, so a real substitution always finds a price.
    price = _latest_price_for_retailer(db, product.id, meal_plan.retailer_id)
    item.product_id = product.id
    if price is not None:
        item.unit_price = price.unit_price
        item.price_product_price_id = price.id
        item.package_quantity = price.package_quantity
        item.package_unit = price.package_unit
        item.price_status = "known"
        if item.packages_selected:
            item.total_cost = price.amount * item.packages_selected
    else:
        item.price_product_price_id = None
        item.price_status = "missing"
    db.flush()
    return {"id": str(item.public_id), "product_id": str(product.public_id)}
