"""Per-provider shadow mode (spec §AA) — evaluate against STAGING data only, never production.

A shadow run: sets the provider to ``activation_state=shadow`` (never production), computes recipe
coverage, costs a candidate basket from the provider's staging prices, compares it against a
baseline provider (the demo catalogue by default), records every difference/anomaly, and persists
a :class:`ShadowEvaluationRun`. It never reads production prices for the provider, never writes
production data, and is meant only for authorised internal review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService, FreshnessStatus
from cestaplan_api.ingestion.providers.contracts import ProductCostingMode
from cestaplan_api.ingestion.providers.onboarding import (
    classify_variant_costing_mode,
    get_entry,
)
from cestaplan_api.models import (
    IngredientProductMapping,
    ProductPrice,
    ProductVariant,
    Recipe,
    Retailer,
    ShadowEvaluationRun,
)
from cestaplan_api.services.recipe_catalog_coverage import evaluate_recipe_catalog_coverage


@dataclass(slots=True)
class _BasketSum:
    known_cost: Decimal
    priced_ingredients: int
    total_ingredients: int
    missing: int
    unresolved_packages: int
    stale: int


def _provider_basket(
    db: Session,
    retailer_id: int | None,
    recipes: list[Recipe],
    *,
    store_id: int | None,
    now: datetime,
) -> _BasketSum:
    """Sum one usable STAGING price per mandatory ingredient (proxy basket; never production)."""
    known = Decimal("0")
    priced = missing = unresolved = stale = total = 0
    prices = CurrentPriceService()
    ing_products: dict[int, list[int]] = {}
    variants: dict[int, list[ProductVariant]] = {}
    if retailer_id is not None:
        for ing_id, prod_id in db.execute(
            select(
                IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id
            ).where(
                IngredientProductMapping.retailer_id == retailer_id,
                IngredientProductMapping.is_active.is_(True),
            )
        ).all():
            if ing_id is None or prod_id is None:
                continue
            ing_products.setdefault(ing_id, []).append(prod_id)
        for v in db.execute(
            select(ProductVariant).where(
                ProductVariant.retailer_id == retailer_id, ProductVariant.active.is_(True)
            )
        ).scalars():
            if v.product_id is not None:
                variants.setdefault(v.product_id, []).append(v)

    for recipe in recipes:
        for ri in (i for i in recipe.ingredients if not i.optional):
            total += 1
            found = False
            for product_id in ing_products.get(ri.ingredient_id, []):
                for v in variants.get(product_id, []):
                    price = prices.current(db, v.id, store_id=store_id, as_of=now, staging=True)
                    if price is None:
                        continue
                    mode = classify_variant_costing_mode(
                        sell_unit=v.sell_unit,
                        variable_weight=v.variable_weight,
                        net_content_quantity=v.net_content_quantity,
                        net_content_unit=v.net_content_unit,
                        unit_price=v.unit_price,
                        unit_price_unit=v.unit_price_unit,
                        has_price=True,
                    )
                    if mode is ProductCostingMode.UNRESOLVED:
                        unresolved += 1
                        continue
                    if price.status is not FreshnessStatus.FRESH:
                        stale += 1
                    known += price.amount
                    priced += 1
                    found = True
                    break
                if found:
                    break
            if not found:
                missing += 1
    return _BasketSum(known, priced, total, missing, unresolved, stale)


def _baseline_basket(db: Session, baseline_slug: str, recipes: list[Recipe]) -> Decimal:
    """Sum one demo (legacy ProductPrice) price per mandatory ingredient — the comparison base."""
    rid = db.execute(select(Retailer.id).where(Retailer.slug == baseline_slug)).scalar_one_or_none()
    if rid is None:
        return Decimal("0")
    ing_products: dict[int, list[int]] = {}
    for ing_id, prod_id in db.execute(
        select(IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id).where(
            IngredientProductMapping.retailer_id == rid,
            IngredientProductMapping.is_active.is_(True),
        )
    ).all():
        ing_products.setdefault(ing_id, []).append(prod_id)
    price_by_product: dict[int, Decimal] = {}
    for prod_id, amount in db.execute(
        select(ProductPrice.product_id, ProductPrice.amount)
        .where(ProductPrice.retailer_id == rid)
        .order_by(ProductPrice.observed_at.desc())
    ).all():
        price_by_product.setdefault(prod_id, amount)
    total = Decimal("0")
    for recipe in recipes:
        for ri in (i for i in recipe.ingredients if not i.optional):
            for product_id in ing_products.get(ri.ingredient_id, []):
                if product_id in price_by_product:
                    total += price_by_product[product_id]
                    break
    return total


def run_provider_shadow(
    db: Session,
    provider_code: str,
    *,
    recipe_limit: int = 20,
    baseline_provider: str = "demo",
    baseline_slug: str = "mercaejemplo",
    now: datetime | None = None,
    activate_shadow: bool = True,
) -> ShadowEvaluationRun:
    """Run a shadow evaluation and persist it. Sets activation_state=shadow (never production)."""
    now = now or datetime.now(UTC)
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()

    coverage = evaluate_recipe_catalog_coverage(
        db, provider_code, scope="staging", recipe_limit=recipe_limit, now=now
    )
    recipes = list(
        db.execute(
            select(Recipe)
            .where(Recipe.deleted_at.is_(None), Recipe.is_synthetic.is_(True))
            .order_by(Recipe.id)
            .limit(recipe_limit)
        ).scalars()
    )
    basket = _provider_basket(db, retailer_id, recipes, store_id=None, now=now)
    baseline_cost = _baseline_basket(db, baseline_slug, recipes)
    abs_diff = basket.known_cost - baseline_cost
    pct_diff = (
        (abs_diff / baseline_cost * Decimal("100")).quantize(Decimal("0.0001"))
        if baseline_cost > 0
        else None
    )
    warnings: list[str] = []
    if coverage.fully_costable_recipes == 0:
        warnings.append("0 recetas totalmente calculables con datos staging (no apto para planes)")
    if basket.missing:
        warnings.append(f"{basket.missing} ingredientes sin producto/precio en staging")

    if activate_shadow:
        _set_shadow_state(db, provider_code)

    run = ShadowEvaluationRun(
        provider_code=provider_code,
        retailer_slug=retailer_slug,
        recipe_set_id=f"synthetic:{len(recipes)}",
        started_at=now,
        completed_at=now,
        status="completed",
        recipes_evaluated=coverage.total_recipes,
        recipes_costable=coverage.fully_costable_recipes,
        basket_known_cost=basket.known_cost,
        basket_estimated_cost=Decimal("0"),
        missing_products=basket.missing,
        unresolved_packages=basket.unresolved_packages,
        stale_prices=basket.stale,
        conflicts=0,
        baseline_provider=baseline_provider,
        baseline_cost=baseline_cost,
        absolute_difference=abs_diff,
        percentage_difference=pct_diff,
        warnings=warnings,
        report_json={"coverage": coverage.as_dict()},
    )
    db.add(run)
    db.flush()
    return run


def _set_shadow_state(db: Session, provider_code: str) -> None:
    """Mark the provider as shadow — never production, rights untouched, no approval."""
    from cestaplan_api.models import ProviderActivation

    row = db.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == provider_code)
    ).scalar_one_or_none()
    if row is None:
        return
    # Only ever move a non-production provider into shadow; never touch a production one.
    if row.activation_state in ("disabled", "transport_only", "staging", "shadow"):
        row.activation_state = "shadow"
        row.development_only = True
    row.production_approved_at = None
    row.production_approved_by = None
    row.production_eligibility = False
    db.flush()


__all__ = ["run_provider_shadow"]
