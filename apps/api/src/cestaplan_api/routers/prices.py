"""PRICES API router (prefix ``/api/v1``): the NutriPlan-facing pricing surface (FASE B §19).

Every route requires an authenticated session. Stores, retailers, variants and products are
addressed by their public UUID. Money and physical quantities are returned as strings.

Endpoints:
- ``GET  /stores/{id}/coverage``        latest honest :class:`CoverageSnapshot`.
- ``GET  /stores/{id}/catalog-status``  discovered/priced/fresh/stale + last crawl run + age.
- ``GET  /products/search``             search variants/products by name (paginated).
- ``GET  /products/{id}/prices``        current price + observation history for a variant.
- ``GET  /prices/current``              scope-aware current price with freshness/age.
- ``POST /prices/resolve-basket``       whole-package, promotion-aware basket costing.

``/retailers`` and ``/retailers/{id}/stores`` already live in ``routers/catalog.py`` and are
intentionally not duplicated here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select

from cestaplan_api.deps import CurrentUser, DbSession
from cestaplan_api.ingestion.coverage import PriceCoverageService
from cestaplan_api.ingestion.current_price import CurrentPriceService, FreshnessStatus
from cestaplan_api.models import (
    CrawlRun,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
)
from cestaplan_api.schemas.prices import (
    ResolveBasketRequest,
    serialize_basket,
)
from cestaplan_api.services.basket_resolver import resolve_basket

router = APIRouter(prefix="/api/v1", tags=["prices"])

_STALE_HOURS = 24
_EXPIRED_HOURS = 48


def _s(value: Any) -> str | None:
    """Serialize a value to a string, rendering Decimals as minimal fixed-point."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _now() -> datetime:
    return datetime.now(UTC)


def _age_seconds(observed_at: datetime, as_of: datetime) -> float:
    return (as_of - observed_at).total_seconds()


def _freshness(age_seconds: float) -> FreshnessStatus:
    hours = age_seconds / 3600.0
    if hours < 0 or hours < _STALE_HOURS:
        return FreshnessStatus.FRESH
    if hours < _EXPIRED_HOURS:
        return FreshnessStatus.STALE
    return FreshnessStatus.EXPIRED


def _load_store(db: DbSession, store_id: uuid.UUID) -> Store:
    store = db.execute(
        select(Store).where(Store.public_id == store_id)
    ).scalar_one_or_none()
    if store is None or not store.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")
    return store


# --------------------------------------------------------------------------- #
# Store coverage / catalog status
# --------------------------------------------------------------------------- #
@router.get("/stores/{store_id}/coverage")
def store_coverage(
    store_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Latest persisted coverage snapshot for a store — honest status, ratios, counts.

    When no snapshot has been computed yet the response is a 200 with ``has_snapshot`` false
    and a ``none`` status: coverage is never dressed up as complete.
    """
    store = _load_store(db, store_id)
    snapshot = PriceCoverageService().latest_coverage(db, store.retailer_id, store.id)
    if snapshot is None:
        return {
            "store_id": str(store.public_id),
            "has_snapshot": False,
            "status": "none",
            "observed_at": None,
            "age_seconds": None,
            "expected_products": 0,
            "discovered_products": 0,
            "priced_products": 0,
            "fresh_prices": 0,
            "stale_prices": 0,
            "estimated_prices": 0,
            "unavailable_products": 0,
            "coverage_ratio": None,
            "weighted_coverage_ratio": None,
        }
    age = _age_seconds(snapshot.observed_at, _now())
    return {
        "store_id": str(store.public_id),
        "has_snapshot": True,
        "status": snapshot.status,
        "observed_at": snapshot.observed_at.isoformat(),
        "age_seconds": age,
        "freshness": _freshness(age).value,
        "expected_products": snapshot.expected_products,
        "discovered_products": snapshot.discovered_products,
        "priced_products": snapshot.priced_products,
        "fresh_prices": snapshot.fresh_prices,
        "stale_prices": snapshot.stale_prices,
        "estimated_prices": snapshot.estimated_prices,
        "unavailable_products": snapshot.unavailable_products,
        "coverage_ratio": _s(snapshot.coverage_ratio),
        "weighted_coverage_ratio": _s(snapshot.weighted_coverage_ratio),
    }


@router.get("/stores/{store_id}/catalog-status")
def store_catalog_status(
    store_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Catalogue health for a store: discovered/priced/fresh/stale counts + last crawl run.

    Counts come from the latest coverage snapshot; ``last_run`` from the most recent crawl
    run for the store (or the chain when the run is not store-scoped). Missing data is
    reported honestly as null/zero, never invented.
    """
    store = _load_store(db, store_id)
    now = _now()
    snapshot = PriceCoverageService().latest_coverage(db, store.retailer_id, store.id)

    run = db.execute(
        select(CrawlRun)
        .where(
            CrawlRun.retailer_id == store.retailer_id,
            or_(CrawlRun.store_id == store.id, CrawlRun.store_id.is_(None)),
        )
        .order_by(CrawlRun.scheduled_at.desc().nullslast(), CrawlRun.id.desc())
        .limit(1)
    ).scalars().first()

    last_run: dict[str, Any] | None = None
    if run is not None:
        marker = run.completed_at or run.started_at or run.scheduled_at
        last_run = {
            "id": str(run.public_id),
            "run_type": run.run_type,
            "status": run.status,
            "scheduled_at": run.scheduled_at.isoformat() if run.scheduled_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "age_seconds": _age_seconds(marker, now) if marker is not None else None,
            "discovered_count": run.discovered_count,
            "accepted_count": run.accepted_count,
            "error_count": run.error_count,
        }

    return {
        "store_id": str(store.public_id),
        "discovered_products": snapshot.discovered_products if snapshot else 0,
        "priced_products": snapshot.priced_products if snapshot else 0,
        "fresh_prices": snapshot.fresh_prices if snapshot else 0,
        "stale_prices": snapshot.stale_prices if snapshot else 0,
        "estimated_prices": snapshot.estimated_prices if snapshot else 0,
        "coverage_status": snapshot.status if snapshot else "none",
        "catalog_updated_at": (
            store.catalog_updated_at.isoformat() if store.catalog_updated_at else None
        ),
        "last_run": last_run,
    }


# --------------------------------------------------------------------------- #
# Product / variant search + price history
# --------------------------------------------------------------------------- #
@router.get("/products/search")
def search_products(
    user: CurrentUser,
    db: DbSession,
    q: str = Query(min_length=1, max_length=200),
    retailer_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Search active product variants by variant display name or canonical product name."""
    pattern = f"%{q.strip()}%"
    base = (
        select(ProductVariant, Product)
        .join(Product, Product.id == ProductVariant.product_id, isouter=True)
        .where(
            ProductVariant.active.is_(True),
            or_(
                ProductVariant.display_name.ilike(pattern),
                Product.name.ilike(pattern),
            ),
        )
    )
    if retailer_id is not None:
        retailer = db.execute(
            select(Retailer.id).where(Retailer.public_id == retailer_id)
        ).scalar_one_or_none()
        base = base.where(ProductVariant.retailer_id == (retailer or -1))

    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = db.execute(
        base.order_by(ProductVariant.display_name, ProductVariant.id)
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    items = [
        {
            "variant_id": str(variant.public_id),
            "display_name": variant.display_name,
            "product_id": str(product.public_id) if product is not None else None,
            "product_name": product.name if product is not None else None,
            "package_quantity": _s(variant.package_quantity),
            "package_unit": variant.package_unit,
            "image_url": variant.image_url,
        }
        for variant, product in rows
    ]
    return {"q": q, "page": page, "size": size, "count": total, "items": items}


@router.get("/products/{variant_id}/prices")
def variant_prices(
    variant_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Current price + append-only observation history for a variant (scope/type/date/age)."""
    variant = db.execute(
        select(ProductVariant).where(ProductVariant.public_id == variant_id)
    ).scalar_one_or_none()
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Variante no encontrada")

    now = _now()
    current = CurrentPriceService().current(db, variant.id, as_of=now)
    observations = db.execute(
        select(PriceObservation)
        .where(PriceObservation.product_variant_id == variant.id)
        .order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc())
        .limit(limit)
    ).scalars().all()

    return {
        "variant_id": str(variant.public_id),
        "display_name": variant.display_name,
        "current": _serialize_current(current) if current is not None else None,
        "history": [
            {
                "amount": _s(obs.amount),
                "currency": obs.currency,
                "price_scope": obs.price_scope,
                "price_type": obs.price_type,
                "observed_at": obs.observed_at.isoformat(),
                "age_seconds": _age_seconds(obs.observed_at, now),
                "valid_from": obs.valid_from.isoformat(),
                "valid_until": obs.valid_until.isoformat() if obs.valid_until else None,
                "available": obs.available,
                "promotion_text": obs.promotion_text,
                "verification_status": obs.verification_status,
            }
            for obs in observations
        ],
    }


# --------------------------------------------------------------------------- #
# Current price
# --------------------------------------------------------------------------- #
def _serialize_current(current: Any) -> dict[str, Any]:
    return {
        "amount": _s(current.amount),
        "currency": current.currency,
        "price_scope": current.price_scope,
        "price_type": current.price_type,
        "store_id": current.store_id,
        "delivery_zone_id": current.delivery_zone_id,
        "source_id": current.source_id,
        "observed_at": current.observed_at.isoformat(),
        "age_seconds": current.age.total_seconds(),
        "freshness": current.status.value,
        "confidence": _s(current.confidence),
        "promotion": current.promotion_text,
        "available": current.available,
    }


@router.get("/prices/current")
def current_price(
    user: CurrentUser,
    db: DbSession,
    variant_id: uuid.UUID,
    store_id: uuid.UUID | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Scope-aware current price for a variant with freshness (fresh/stale/expired)."""
    variant = db.execute(
        select(ProductVariant.id).where(ProductVariant.public_id == variant_id)
    ).scalar_one_or_none()
    if variant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Variante no encontrada")

    store_internal: int | None = None
    if store_id is not None:
        store_internal = _load_store(db, store_id).id

    current = CurrentPriceService().current(
        db, variant, store_id=store_internal, scope=scope, as_of=_now()
    )
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sin precio disponible")
    return _serialize_current(current)


# --------------------------------------------------------------------------- #
# Resolve basket — the key endpoint
# --------------------------------------------------------------------------- #
@router.post("/prices/resolve-basket")
def resolve_basket_endpoint(
    payload: ResolveBasketRequest, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Cost a NutriPlan basket: whole packages, promotions, honest coverage + unresolved.

    Prices are read scope-aware from the append-only history; a store scopes prices to that
    store, a chain aggregates across the chain. Missing prices/matches are returned as
    ``unresolved`` — never fabricated.
    """
    store_public: uuid.UUID | None = None
    if payload.store_id is not None:
        store = _load_store(db, payload.store_id)
        retailer_internal = store.retailer_id
        store_internal: int | None = store.id
        store_public = store.public_id
        retailer_public = db.execute(
            select(Retailer.public_id).where(Retailer.id == store.retailer_id)
        ).scalar_one()
    else:
        retailer = db.execute(
            select(Retailer).where(Retailer.public_id == payload.retailer_id)
        ).scalar_one_or_none()
        if retailer is None or not retailer.is_active:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Distribuidor no encontrado"
            )
        retailer_internal = retailer.id
        store_internal = None
        retailer_public = retailer.public_id

    resolution = resolve_basket(
        db,
        retailer_id=retailer_internal,
        store_id=store_internal,
        retailer_public_id=retailer_public,
        store_public_id=store_public,
        items=[item.to_item() for item in payload.items],
        as_of=_now(),
        currency=payload.currency,
    )
    body = serialize_basket(resolution)
    body["target_date"] = (
        payload.target_date.isoformat() if payload.target_date is not None else None
    )
    return body
