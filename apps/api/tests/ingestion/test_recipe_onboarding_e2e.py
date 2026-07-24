"""End-to-end onboarding of ONE recipe to fully-costable (spec §12) — DB-backed, no network.

The whole pipeline on a throwaway provider/retailer, deterministically and hermetically:

  pre-approve one ingredient -> discover two -> enrich -> deterministic auto-approval ->
  full quantity-aware costing -> comparable per-recipe shadow -> revoke a mapping ->
  recipe is no longer costable, and production data is untouched throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    PriceScope,
    SellUnit,
)
from cestaplan_api.models import (
    ExternalProduct,
    IngredientProductMapping,
    PriceObservation,
    Product,
    ProductPrice,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
    Store,
    User,
)
from cestaplan_api.services import mapping_enrichment as enr
from cestaplan_api.services import targeted_discovery as td
from cestaplan_api.services.ingredient_dictionary import (
    classify_mapping,
    normalize_provider_category,
)
from cestaplan_api.services.mapping_review import revoke
from cestaplan_api.services.recipe_costing import cost_recipe
from cestaplan_api.services.recipe_shadow import RecipeShadowStatus, compare_recipe_shadow

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
_E2E = "test-e2e-prov"
_BASE = "test-e2e-base"
_AVENA, _LECHE, _PLATANO = 792, 779, 760


def _settings() -> Settings:
    return Settings(enrichment_daily_budget=50, enrichment_min_seconds_between=5)


def _user(db: Session) -> int:
    u = User(email=f"e2e-{id(db)}@x.com", password_hash="x", display_name="E2E")
    db.add(u)
    db.flush()
    return u.id


def _product(
    db: Session, ext: str, name: str, qty: str, unit: ContentUnit, price: str
) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider=_E2E,
        retailer_slug=_E2E,
        external_product_id=ext,
        product_name=name,
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal(price),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        variable_weight=False,
        brand=None,
        category="Frutas" if "látano" in name else ("Lácteos" if "eche" in name else "Cereales"),
        net_content_quantity=Decimal(qty),
        net_content_unit=unit,
    )


def _discover_one(
    db: Session, rid: int, ing_id: int, key: str, product: ExternalCatalogProduct
) -> ProviderIngredientMapping:
    """Drive the genuine discovery internals for one product (classify -> persist -> map)."""
    cand = classify_mapping(
        key,
        product_name=product.product_name,
        brand=product.brand,
        category_code=normalize_provider_category(product.category),
        net_content_unit=product.net_content_unit.value if product.net_content_unit else None,
    )
    pid, _vid = td._persist_product(db, rid, product, now=_NOW)
    active = cand.mapping_status == "auto_approved"
    td._upsert_mapping(db, _E2E, _E2E, ing_id, key, product, pid, cand, active=active, now=_NOW)
    return db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == _E2E,
            ProviderIngredientMapping.external_product_id == product.external_product_id,
        )
    ).scalar_one()


def _preapprove_avena(db: Session, rid: int) -> None:
    """The one already-approved ingredient (approved before this run)."""
    _discover_one(
        db,
        rid,
        _AVENA,
        "avena_copos",
        _product(db, "E2E-AVENA", "Copos de avena integrales 500 g", "500", ContentUnit.G, "0.75"),
    )


def _baseline(db: Session) -> None:
    r = Retailer(slug=_BASE, name="E2E Base", adapter_key="demo", is_synthetic=True)
    db.add(r)
    db.flush()
    store = Store(retailer_id=r.id, name="Base Store", is_synthetic=True)
    db.add(store)
    db.flush()
    for ing_id, name, qty, unit, price in [
        (_AVENA, "Copos demo 500g", "500", "g", "1.06"),
        (_LECHE, "Leche demo 1L", "1000", "ml", "0.76"),
        (_PLATANO, "Plátano demo 1kg", "1", "kg", "1.32"),
    ]:
        product = Product(
            name=name, is_synthetic=True, package_quantity=Decimal(qty), package_unit=unit
        )
        db.add(product)
        db.flush()
        db.add(
            ProductPrice(
                retailer_id=r.id,
                store_id=store.id,
                product_id=product.id,
                amount=Decimal(price),
                currency="EUR",
                package_quantity=Decimal(qty),
                package_unit=unit,
                source_type="demo",
                source_name="demo",
                observed_at=_NOW,
                imported_at=_NOW,
                confidence_score=Decimal("1.0"),
            )
        )
        db.add(
            IngredientProductMapping(
                retailer_id=r.id, ingredient_id=ing_id, product_id=product.id, is_active=True
            )
        )
    db.flush()


def _recipe(db: Session) -> Recipe:
    recipe = Recipe(origin="seed", title="Porridge E2E", servings=2, is_synthetic=True)
    db.add(recipe)
    db.flush()
    for ing_id, key, qty, unit in [
        (_AVENA, "avena_copos", "80", "g"),
        (_LECHE, "leche_entera", "400", "ml"),
        (_PLATANO, "platano", "160", "g"),
    ]:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=ing_id,
                canonical_name=key,
                display_name=key,
                quantity=Decimal(qty),
                unit=unit,
                optional=False,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def test_recipe_onboarding_end_to_end(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # Enrichment endpoint for the throwaway provider (injected fetcher -> no network).
    monkeypatch.setitem(enr._DETAIL_ENDPOINT, _E2E, "/detail")

    retailer = Retailer(slug=_E2E, name="E2E Prov", adapter_key="test", is_synthetic=True)
    db_session.add(retailer)
    db_session.flush()
    rid = retailer.id
    uid = _user(db_session)  # one reviewer/enricher for the whole flow

    # 1) One ingredient pre-approved.
    _preapprove_avena(db_session, rid)

    # 2) Two ingredients discovered -> deterministic auto-approval (leche multi-term; plátano
    #    single-word promoted by its normalised 'frutas' category).
    leche = _discover_one(
        db_session,
        rid,
        _LECHE,
        "leche_entera",
        _product(
            db_session, "E2E-LECHE", "Leche entera de vaca 1 L", "1000", ContentUnit.ML, "0.95"
        ),
    )
    platano = _discover_one(
        db_session,
        rid,
        _PLATANO,
        "platano",
        _product(
            db_session,
            "E2E-PLAT",
            "Plátano de Canarias bandeja 700 g",
            "700",
            ContentUnit.G,
            "2.49",
        ),
    )
    assert leche.mapping_status == "auto_approved" and leche.active
    assert platano.mapping_status == "auto_approved" and platano.active

    # 3) Enrich the plátano candidate (single audited detail call via injected fetcher).
    def _fetcher(
        provider_code: str, external_product_id: str, settings: Settings
    ) -> dict[str, Any]:
        return {"category": "frutas", "unit": "g", "net_content": "700 g"}

    enr.enrich(
        db_session,
        platano.id,
        requested_by=uid,
        settings=_settings(),
        now=_NOW,
        detail_fetcher=_fetcher,
    )
    db_session.refresh(platano)
    assert platano.enrichment_status == "completed"
    assert platano.active is True  # enrichment preserves the deterministic approval

    # 4) Full quantity-aware costing -> fully costable.
    recipe = _recipe(db_session)
    costing = cost_recipe(db_session, recipe, _E2E, now=_NOW)
    assert costing.fully_costable is True
    assert costing.total_purchase_cost == Decimal("4.19")  # 0.75 + 0.95 + 2.49
    assert costing.cost_per_serving_purchase == Decimal("2.10")

    # 5) Comparable per-recipe shadow vs the baseline demo catalogue.
    _baseline(db_session)
    shadow = compare_recipe_shadow(db_session, recipe, _E2E, baseline_slug=_BASE, now=_NOW)
    assert shadow.comparison_status == RecipeShadowStatus.COMPARABLE.value
    assert shadow.provider_cost == Decimal("4.19")
    assert shadow.baseline_cost == Decimal("3.14")  # 1.06 + 0.76 + 1.32
    assert shadow.absolute_difference == Decimal("1.05")

    # 6) Revoke one mapping -> the recipe is no longer costable...
    revoke(db_session, leche.id, reviewer_id=uid, reason="test revoke", now=_NOW)
    after = cost_recipe(db_session, recipe, _E2E, now=_NOW)
    assert after.fully_costable is False
    assert after.total_purchase_cost is None
    assert any("leche" in r for r in after.uncostable_reasons)

    # ...and production data was never written by any step (staging only).
    non_staging = db_session.execute(
        select(func.count())
        .select_from(PriceObservation)
        .join(ProductVariant, ProductVariant.id == PriceObservation.product_variant_id)
        .where(ProductVariant.retailer_id == rid, PriceObservation.staging_only.is_(False))
    ).scalar()
    assert non_staging == 0

    # The baseline (demo) side is unaffected by the revoke — it is still independently costable.
    from cestaplan_api.services.recipe_shadow import _cost_baseline

    assert _cost_baseline(db_session, recipe, _BASE).fully_costable is True


def _count_variants_for(db: Session, rid: int) -> int:
    return (
        db.execute(
            select(func.count())
            .select_from(ProductVariant)
            .where(ProductVariant.retailer_id == rid)
        ).scalar()
        or 0
    )


def test_discovery_creates_external_products_and_variants(db_session: Session) -> None:
    retailer = Retailer(slug=_E2E, name="E2E Prov", adapter_key="test", is_synthetic=True)
    db_session.add(retailer)
    db_session.flush()
    _discover_one(
        db_session,
        retailer.id,
        _PLATANO,
        "platano",
        _product(db_session, "E2E-P2", "Plátano de Canarias 700 g", "700", ContentUnit.G, "2.49"),
    )
    assert _count_variants_for(db_session, retailer.id) == 1
    ext = (
        db_session.execute(
            select(ExternalProduct).where(ExternalProduct.retailer_id == retailer.id)
        )
        .scalars()
        .all()
    )
    assert len(ext) == 1
