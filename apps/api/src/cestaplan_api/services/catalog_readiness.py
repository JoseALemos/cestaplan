"""Planner catalog-readiness report for the admin panel.

Answers "can the planner actually build a costed plan yet, and if not, what is missing?" from the
productive catalogue — recipes, ingredient mappings, products, prices, staging and provider
activation. The global status is NEVER ``available`` unless at least one provider is both
``production_enabled`` AND ``production_approved`` (staging alone is never production).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    ExternalProduct,
    Ingredient,
    IngredientProductMapping,
    PriceObservation,
    Product,
    ProductPrice,
    ProviderActivation,
    Recipe,
    Retailer,
)
from cestaplan_api.services.planner_preflight import _count_costable_recipes


class ReadinessStatus(StrEnum):
    NO_RECIPES = "no_recipes"
    NO_CATALOG = "no_catalog"
    NO_PRICES = "no_prices"
    PENDING_MAPPINGS = "pending_mappings"
    STAGING_ONLY = "staging_only"
    READY_FOR_REVIEW = "ready_for_review"
    AVAILABLE = "available"


def _c(db: Session, model: Any, *where: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def catalog_readiness_report(db: Session) -> dict[str, Any]:
    recipes_active = _c(db, Recipe, Recipe.is_public.is_(True))
    recipes_costable = _count_costable_recipes(db, None) if recipes_active else 0
    ingredients = _c(db, Ingredient)
    approved_mappings = _c(
        db, IngredientProductMapping, IngredientProductMapping.is_active.is_(True)
    )
    productive_products = _c(db, Product, Product.deleted_at.is_(None))
    productive_prices = _c(db, ProductPrice)
    staging_products = _c(db, ExternalProduct)
    staging_observations = _c(db, PriceObservation, PriceObservation.staging_only.is_(True))
    chains_available = int(
        db.scalar(select(func.count(func.distinct(ProductPrice.retailer_id)))) or 0
    )
    total_chains = _c(db, Retailer)
    production_ready_providers = _c(
        db,
        ProviderActivation,
        ProviderActivation.production_enabled.is_(True),
        ProviderActivation.production_approved.is_(True),
    )
    last_sync = db.scalar(
        select(func.max(func.coalesce(CrawlRun.completed_at, CrawlRun.started_at)))
    )

    blockers: list[str] = []
    if recipes_active == 0:
        blockers.append("no_active_recipes")
    if approved_mappings == 0:
        blockers.append("no_mapped_products")
    if productive_prices == 0:
        blockers.append("no_product_prices")
    if recipes_active and recipes_costable == 0:
        blockers.append("no_costable_recipes")
    if production_ready_providers == 0:
        blockers.append("no_production_approved_provider")

    status = _global_status(
        recipes_active=recipes_active,
        productive_products=productive_products,
        staging_products=staging_products,
        staging_observations=staging_observations,
        approved_mappings=approved_mappings,
        productive_prices=productive_prices,
        recipes_costable=recipes_costable,
        production_ready_providers=production_ready_providers,
    )

    return {
        "status": status.value,
        "recipes_active": recipes_active,
        "recipes_costable": recipes_costable,
        "ingredients": ingredients,
        "approved_mappings": approved_mappings,
        "productive_products": productive_products,
        "productive_prices": productive_prices,
        "staging_products": staging_products,
        "staging_observations": staging_observations,
        "chains_available": chains_available,
        "total_chains": total_chains,
        "production_ready_providers": production_ready_providers,
        "last_sync_at": last_sync.isoformat() if last_sync is not None else None,
        # When THIS snapshot was computed, so the UI can show its age and never present a stale view
        # as current.
        "fetched_at": datetime.now(UTC).isoformat(),
        "blockers": blockers,
    }


def _global_status(
    *,
    recipes_active: int,
    productive_products: int,
    staging_products: int,
    staging_observations: int,
    approved_mappings: int,
    productive_prices: int,
    recipes_costable: int,
    production_ready_providers: int,
) -> ReadinessStatus:
    if recipes_active == 0:
        return ReadinessStatus.NO_RECIPES
    if productive_products == 0 and staging_products == 0 and staging_observations == 0:
        return ReadinessStatus.NO_CATALOG
    if approved_mappings == 0:
        return ReadinessStatus.PENDING_MAPPINGS
    if productive_prices == 0 and staging_observations > 0:
        return ReadinessStatus.STAGING_ONLY
    if productive_prices == 0:
        return ReadinessStatus.NO_PRICES
    if recipes_costable == 0:
        return ReadinessStatus.PENDING_MAPPINGS
    # Costable catalogue exists, but the planner only serves it once a provider is production-
    # enabled AND production-approved. Never report "available" on staging/shadow alone.
    if production_ready_providers == 0:
        return ReadinessStatus.READY_FOR_REVIEW
    return ReadinessStatus.AVAILABLE


__all__ = ["ReadinessStatus", "catalog_readiness_report"]
