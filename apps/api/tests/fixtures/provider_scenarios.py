"""Hermetic test fixtures for provider/ingredient scenarios.

These helpers let a test create EXPLICITLY, inside its own rolled-back transaction, everything it
needs (ingredients, retailers, provider activations, external/normalized products, staging prices,
mapping candidates, conflicts, recipes, provider usage). No test should depend on ambient dev-DB
data, on ``seed_demo``, on execution order, or on numeric ids from an existing database.

Every helper:
* uses DB-generated ids (never hard-codes them);
* returns the created object(s);
* is idempotent (get-or-create) when invoked twice for the same logical entity;
* runs inside the caller's transaction and is rolled back on teardown;
* is deterministic (a fixed timestamp is used where one is needed).

They are plain functions (not autouse fixtures): each test requests exactly the scenario it needs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.onboarding import get_entry, upsert_activation
from cestaplan_api.models import (
    ExternalProduct,
    Ingredient,
    PriceObservation,
    Product,
    ProductVariant,
    ProviderActivation,
    ProviderIngredientMapping,
    ProviderUsage,
    Recipe,
    RecipeIngredient,
    Retailer,
    Store,
)

# A fixed, deterministic timestamp (helpers never call an arg-less ``datetime.now``).
FIXED_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def ensure_test_ingredient(
    db: Session,
    canonical_name: str,
    *,
    category_code: str | None = None,
    display_name: str | None = None,
    default_unit: str | None = None,
    allergen_codes: list[str] | None = None,
) -> Ingredient:
    """Get-or-create a canonical ingredient by name (never a ``scalar_one`` on ambient rows)."""
    row = db.execute(
        select(Ingredient).where(Ingredient.canonical_name == canonical_name)
    ).scalar_one_or_none()
    if row is None:
        row = Ingredient(
            canonical_name=canonical_name,
            display_name=display_name or canonical_name.replace("_", " ").title(),
            category_code=category_code,
            default_unit=default_unit,
            allergen_codes=allergen_codes or [],
            is_synthetic=True,
        )
        db.add(row)
        db.flush()
    return row


def seed_test_ingredient_vocabulary(
    db: Session, specs: list[tuple[str, str | None]]
) -> dict[str, Ingredient]:
    """Create a minimal synthetic vocabulary: ``[(canonical_name, category_code), ...]``."""
    return {name: ensure_test_ingredient(db, name, category_code=cat) for name, cat in specs}


def seed_test_canonical_ingredients(db: Session) -> dict[str, Ingredient]:
    """Create the full canonical ingredient vocabulary from the real spec data (no demo catalogue).

    Reuses ``cestaplan_api.seed.data.INGREDIENTS`` (name/category/unit/allergens) so a test that
    needs the whole vocabulary (e.g. an ``ingredients_total == 75`` assertion, or the matcher over
    many products) gets it hermetically — WITHOUT seeding demo products/prices/recipes.
    """
    from cestaplan_api.seed.data import INGREDIENTS

    out: dict[str, Ingredient] = {}
    for spec in INGREDIENTS:
        out[spec["name"]] = ensure_test_ingredient(
            db,
            spec["name"],
            category_code=spec["cat"],
            display_name=spec["display"],
            default_unit=spec["unit"],
            allergen_codes=list(spec["allergens"]) or None,
        )
    return out


def seed_test_retailer(
    db: Session, slug: str, *, name: str | None = None, adapter_key: str = "test"
) -> Retailer:
    """Get-or-create a retailer by slug."""
    row = db.execute(select(Retailer).where(Retailer.slug == slug)).scalar_one_or_none()
    if row is None:
        row = Retailer(
            slug=slug,
            name=name or slug.replace("-", " ").title(),
            adapter_key=adapter_key,
            country="ES",
            is_synthetic=True,
        )
        db.add(row)
        db.flush()
    return row


def seed_test_store(db: Session, retailer: Retailer, *, name: str = "Tienda test") -> Store:
    row = db.execute(
        select(Store).where(Store.retailer_id == retailer.id, Store.name == name)
    ).scalar_one_or_none()
    if row is None:
        row = Store(retailer_id=retailer.id, name=name, is_synthetic=True)
        db.add(row)
        db.flush()
    return row


def seed_test_provider_activation(
    db: Session,
    provider_code: str,
    *,
    now: datetime = FIXED_NOW,
    **overrides: object,
) -> ProviderActivation:
    """Create the retailer + a ProviderActivation from the real onboarding matrix.

    Rights stay ``under_review`` and production stays OFF (upsert_activation never grants it).
    ``overrides`` set explicit gate/state values the scenario needs (e.g. staging_enabled=True).
    """
    entry = get_entry(provider_code)
    if entry is None:  # unknown provider: still create a bare activation row
        seed_test_retailer(db, provider_code)
        row = db.execute(
            select(ProviderActivation).where(ProviderActivation.provider_code == provider_code)
        ).scalar_one_or_none() or ProviderActivation(provider_code=provider_code)
        db.add(row)
    else:
        seed_test_retailer(db, entry.retailer_slug, name=entry.retailer_slug.title())
        row = upsert_activation(db, entry, now=now)
    for key, value in overrides.items():
        setattr(row, key, value)
    db.flush()
    return row


def seed_test_catalog_product(
    db: Session,
    retailer: Retailer,
    external_id: str,
    *,
    name: str,
    net_qty: str | None = None,
    net_unit: str | None = None,
    price: str | None = None,
    staging: bool = True,
    variable_weight: bool = False,
    sell_unit: str = "package",
    unit_price: str | None = None,
    unit_price_unit: str | None = None,
    now: datetime = FIXED_NOW,
) -> tuple[Product, ProductVariant]:
    """Create Product + ExternalProduct + ProductVariant (+ optional staging price)."""
    product = Product(name=name, is_synthetic=False)
    db.add(product)
    db.flush()
    ext = ExternalProduct(retailer_id=retailer.id, external_id=external_id)
    db.add(ext)
    db.flush()
    variant = ProductVariant(
        retailer_id=retailer.id,
        external_product_id=ext.id,
        product_id=product.id,
        display_name=name,
        sell_unit=sell_unit,
        variable_weight=variable_weight,
        net_content_quantity=Decimal(net_qty) if net_qty is not None else None,
        net_content_unit=net_unit,
        unit_price=Decimal(unit_price) if unit_price is not None else None,
        unit_price_unit=unit_price_unit,
    )
    db.add(variant)
    db.flush()
    if price is not None:
        seed_test_price_observation(db, variant, amount=price, staging=staging, now=now)
    return product, variant


def seed_test_price_observation(
    db: Session,
    variant: ProductVariant,
    *,
    amount: str,
    staging: bool = True,
    scope: str = "national",
    now: datetime = FIXED_NOW,
) -> PriceObservation:
    obs = PriceObservation(
        retailer_id=variant.retailer_id,
        product_variant_id=variant.id,
        price_scope=scope,
        price_type="regular",
        amount=Decimal(amount),
        currency="EUR",
        observed_at=now,
        imported_at=now,
        valid_from=now,
        confidence_score=Decimal("1.0"),
        staging_only=staging,
    )
    db.add(obs)
    db.flush()
    return obs


def seed_test_mapping_candidate(
    db: Session,
    provider_code: str,
    ingredient: Ingredient,
    external_product_id: str,
    *,
    retailer_slug: str,
    mapping_status: str = "candidate",
    active: bool = False,
    relation_status: str = "independent",
    conflict_group_id: str | None = None,
    normalized_product_id: int | None = None,
    confidence: str = "0.8",
    mapping_method: str = "exact_alias",
    required_review: bool = True,
    evidence_json: dict | None = None,
) -> ProviderIngredientMapping:
    """Create one auditable ProviderIngredientMapping row."""
    row = ProviderIngredientMapping(
        provider_code=provider_code,
        ingredient_id=ingredient.id,
        canonical_ingredient_key=ingredient.canonical_name,
        retailer_slug=retailer_slug,
        external_product_id=external_product_id,
        normalized_product_id=normalized_product_id,
        mapping_status=mapping_status,
        mapping_method=mapping_method,
        confidence_score=Decimal(confidence),
        relation_status=relation_status,
        conflict_group_id=conflict_group_id,
        required_review=required_review,
        active=active,
        evidence_json=evidence_json or {"product_name": ingredient.canonical_name},
    )
    db.add(row)
    db.flush()
    return row


def seed_test_conflict_group(
    db: Session,
    provider_code: str,
    retailer_slug: str,
    external_product_id: str,
    ingredients: list[Ingredient],
) -> list[ProviderIngredientMapping]:
    """One external product claimed by MULTIPLE ingredients (a competing/conflict group)."""
    gid = f"{provider_code}:{external_product_id}"
    return [
        seed_test_mapping_candidate(
            db,
            provider_code,
            ing,
            external_product_id,
            retailer_slug=retailer_slug,
            relation_status="competing",
            conflict_group_id=gid,
        )
        for ing in ingredients
    ]


def seed_test_recipe(
    db: Session,
    title: str,
    ingredients: list[tuple[Ingredient, str, str, bool]],
    *,
    servings: int = 2,
    is_synthetic: bool = True,
    origin: str = "seed",
) -> Recipe:
    """Create a Recipe + its RecipeIngredients (``[(ingredient, qty, unit, optional), ...]``)."""
    recipe = Recipe(origin=origin, title=title, servings=servings, is_synthetic=is_synthetic)
    db.add(recipe)
    db.flush()
    for ing, qty, unit, optional in ingredients:
        db.add(
            RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=ing.id,
                canonical_name=ing.canonical_name,
                display_name=ing.canonical_name,
                quantity=Decimal(qty),
                unit=unit,
                optional=optional,
            )
        )
    db.flush()
    db.refresh(recipe)
    return recipe


def seed_test_provider_usage(
    db: Session,
    provider_code: str,
    *,
    operation: str = "targeted_discovery",
    now: datetime = FIXED_NOW,
) -> ProviderUsage:
    row = ProviderUsage(
        provider=provider_code,
        operation=operation,
        request_count=1,
        product_count=1,
        started_at=now,
        completed_at=now,
    )
    db.add(row)
    db.flush()
    return row


__all__ = [
    "FIXED_NOW",
    "ensure_test_ingredient",
    "seed_test_canonical_ingredients",
    "seed_test_catalog_product",
    "seed_test_conflict_group",
    "seed_test_ingredient_vocabulary",
    "seed_test_mapping_candidate",
    "seed_test_price_observation",
    "seed_test_provider_activation",
    "seed_test_provider_usage",
    "seed_test_recipe",
    "seed_test_retailer",
    "seed_test_store",
]
