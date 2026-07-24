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
from cestaplan_api.ingestion.providers.onboarding import RETAILER_MATRIX
from cestaplan_api.ingestion.providers.rights import SourceRights, get_source_rights
from cestaplan_api.models import (
    HouseholdMember,
    Ingredient,
    IngredientProductMapping,
    PriceObservation,
    Product,
    ProductBarcode,
    ProductPrice,
    ProviderActivation,
    Recipe,
    Retailer,
    Store,
)
from cestaplan_api.schemas.catalog import PriceProviderOut, RightsScopeOut
from cestaplan_api.services.open_prices_sync import ensure_open_prices_data_source

router = APIRouter(prefix="/api/v1", tags=["catalog"])

# Package units the engine can cost a recipe against (mass/volume). Products priced
# in a counted unit ("unit") or with no net content — typical of sparse real-chain
# data from Open Prices — cannot be turned into a per-ingredient cost.
_COSTABLE_UNITS = ("g", "kg", "mg", "ml", "l", "cl")
# A retailer needs at least this many ingredients priced in a costable unit before we
# advertise it as able to cost whole plans (vs. being only a real-price viewer).
_COSTING_MIN_INGREDIENTS = 20


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


# All badge strings the UI understands (spec §16). "Sin cobertura" and "Bloqueado por
# autenticación" are reserved for providers that report those states (none do yet).
PROVIDER_BADGES = (
    "Disponible para validación",
    "Experimental",
    "Ofertas solamente",
    "Configuración pendiente",
    "Fuente insuficiente",
    "Sin cobertura",
    "Bloqueado por autenticación",
)


def _provider_badge(entry: Any, activation: ProviderActivation | None) -> str:
    """UI badge for a chain (§16). Reflects OBSERVED costing eligibility, never intent.

    A chain reads "Disponible para validación" (usable to cost a basket) only when its measured
    ``costing_eligibility`` is ``sufficient``. A configured source whose sample is priced but not
    costable stays "Experimental"; an offers-only (partial) source is "Ofertas solamente"; a
    source whose API works but lacks costing-critical fields is "Fuente insuficiente". Nothing is
    dressed up as available on the strength of a handful of records.
    """
    if activation is None or activation.transport_status in ("down", "unknown"):
        return "Configuración pendiente"
    if activation.mapper_status == "blocked":
        return "Fuente insuficiente"  # API reachable but schema lacks costing-critical fields
    if entry.intended_catalog_scope == "partial":
        return "Ofertas solamente"
    if activation.costing_eligibility == "sufficient":
        return "Disponible para validación"
    # Configured/captured but coverage is insufficient to cost plans -> experimental.
    return "Experimental"


def _effective_rights(
    activation: ProviderActivation | None, rights: SourceRights | None
) -> dict[str, Any]:
    """Merge the recorded activation rights with the canonical registry declaration.

    A decided DB value (operator/bootstrap set) wins; otherwise the canonical registry value is
    used so the authorized state shows correctly even before the bootstrap has run. This function
    touches ONLY the legal-rights axis — never production, costing, quality or coverage.
    """

    def act(name: str) -> Any:
        return getattr(activation, name) if activation is not None else None

    db_status = act("data_rights_status")
    if db_status not in (None, "unknown", "under_review"):
        data_rights_status = db_status
    elif rights is not None:
        data_rights_status = rights.data_rights_status
    else:
        data_rights_status = db_status or "under_review"

    db_auth = act("authorization_status")
    if db_auth not in (None, "unknown"):
        authorization_status = db_auth
    elif rights is not None:
        authorization_status = rights.authorization_status
    else:
        authorization_status = db_auth or "unknown"

    def merged(name: str) -> Any:
        value = act(name)
        if value is not None:
            return value
        return getattr(rights, name) if rights is not None else None

    scope = merged("rights_scope")
    return {
        "data_rights_status": data_rights_status,
        "authorization_status": authorization_status,
        "license_basis": merged("license_basis"),
        "license_display_name": merged("license_display_name"),
        "rights_display_name": merged("rights_display_name"),
        "rights_scope": scope,
        "attribution_text_public": merged("attribution_text_public"),
        "attribution_required": (scope or {}).get("attribution_required"),
        # valid_from / valid_until are operator-set only (no registry default).
        "valid_from": act("valid_from"),
        "valid_until": act("valid_until"),
    }


@router.get("/price-providers", response_model=list[PriceProviderOut])
def list_price_providers(user: CurrentUser, db: DbSession) -> list[PriceProviderOut]:
    """Every declared price source with rights, technical status, coverage and costing (§6/§7).

    Legal authorization is reported on its own axis (``authorization_status`` / ``rights_scope`` /
    display names) and NEVER gates visibility here: an authorized-but-incomplete source shows as
    authorized + experimental, not legally blocked. An intermediary technical provider
    (Parse.bot / Apify) is never presented as an official API. Internal evidence/notes are never
    exposed. Nothing here invents data the source does not provide.
    """
    activations = {
        a.provider_code: a for a in db.execute(select(ProviderActivation)).scalars()
    }
    retailers = {r.slug: r for r in db.execute(select(Retailer)).scalars()}
    # Most recent real observation per chain (None when the catalogue has no prices yet).
    _latest_rows = db.execute(
        select(PriceObservation.retailer_id, func.max(PriceObservation.observed_at)).group_by(
            PriceObservation.retailer_id
        )
    ).all()
    latest_by_retailer: dict[int, Any] = {row[0]: row[1] for row in _latest_rows}
    out: list[PriceProviderOut] = []
    for entry in RETAILER_MATRIX:
        activation = activations.get(entry.provider_code)
        retailer = retailers.get(entry.retailer_slug)
        rights = get_source_rights(entry.provider_code)
        eff = _effective_rights(activation, rights)
        latest = latest_by_retailer.get(retailer.id) if retailer is not None else None
        authorized = eff["authorization_status"] == "verified"
        out.append(
            PriceProviderOut(
                provider=entry.provider_code,
                provider_display_name=(
                    rights.provider_display_name if rights else entry.provider_code
                ),
                retailer=entry.retailer_slug,
                retailer_display_name=(
                    rights.retailer_display_name if rights else entry.retailer_slug
                ),
                retailer_id=str(retailer.public_id) if retailer is not None else None,
                technical_provider=rights.technical_provider if rights else None,
                source_type=rights.source_type if rights else "unknown",
                source_url=rights.source_url if rights else None,
                official_api=rights.official_api if rights else False,
                authorized_source=authorized,
                authorization_status=eff["authorization_status"],
                data_rights_status=eff["data_rights_status"],
                rights_scope=(
                    RightsScopeOut(**eff["rights_scope"]) if eff["rights_scope"] else None
                ),
                license_basis=eff["license_basis"],
                license_display_name=eff["license_display_name"],
                rights_display_name=eff["rights_display_name"],
                public_authorization_text=(
                    rights.public_authorization_text if rights else None
                ),
                attribution_required=eff["attribution_required"],
                attribution_text_public=eff["attribution_text_public"],
                valid_from=eff["valid_from"],
                valid_until=eff["valid_until"],
                intended_role=entry.intended_role,
                intended_catalog_scope=entry.intended_catalog_scope,
                observed_catalog_scope=(
                    activation.observed_catalog_scope if activation else "unknown"
                ),
                transport_status=activation.transport_status if activation else "unknown",
                mapper_status=activation.mapper_status if activation else "unknown",
                data_quality_status=(
                    activation.data_quality_status if activation else "unknown"
                ),
                activation_state=activation.activation_state if activation else "disabled",
                price_coverage=_s(activation.price_coverage) if activation else None,
                package_quantity_coverage=(
                    _s(activation.package_quantity_coverage) if activation else None
                ),
                package_unit_coverage=(
                    _s(activation.package_unit_coverage) if activation else None
                ),
                geographic_scope_coverage=(
                    _s(activation.geographic_scope_coverage) if activation else None
                ),
                package_coverage=_s(activation.package_coverage) if activation else None,
                variable_weight_coverage=(
                    _s(activation.variable_weight_coverage) if activation else None
                ),
                unresolved_costing_coverage=(
                    _s(activation.unresolved_costing_coverage) if activation else None
                ),
                costing_eligible_product_coverage=(
                    _s(activation.costing_eligible_product_coverage) if activation else None
                ),
                costing_eligibility=(
                    activation.costing_eligibility if activation else "unknown"
                ),
                production_eligibility=(
                    bool(activation.production_eligibility) if activation else False
                ),
                production_enabled=(
                    bool(activation.production_enabled) if activation else False
                ),
                production_approved=(
                    bool(activation.production_approved) if activation else False
                ),
                badge=_provider_badge(entry, activation),
                available_fields=sorted(entry.capabilities),
                latest_observation_at=latest,
                metadata_status="recorded" if authorized and eff["rights_scope"] else "pending",
            )
        )
    return out


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

    # How many distinct ingredients each retailer prices in a costable (mass/volume)
    # unit — the basis for whether it can cost whole plans or is only a price viewer.
    costable_rows = db.execute(
        select(
            ProductPrice.retailer_id,
            func.count(func.distinct(IngredientProductMapping.ingredient_id)),
        )
        .join(Product, Product.id == ProductPrice.product_id)
        .join(
            IngredientProductMapping,
            IngredientProductMapping.product_id == Product.id,
        )
        .where(
            IngredientProductMapping.is_active.is_(True),
            Product.deleted_at.is_(None),
            func.lower(ProductPrice.package_unit).in_(_COSTABLE_UNITS),
        )
        .group_by(ProductPrice.retailer_id)
    ).all()
    costable_by_retailer: dict[int, int] = {row[0]: row[1] for row in costable_rows}

    result: list[dict[str, Any]] = []
    for r in retailers:
        costable = costable_by_retailer.get(r.id, 0)
        result.append(
            {
                "id": str(r.public_id),
                "name": r.name,
                "is_synthetic": r.is_synthetic,
                # True: prices enough ingredients to cost a plan. False: real-price
                # viewer only (sparse chain data), so plans show low coverage.
                "costing_supported": costable >= _COSTING_MIN_INGREDIENTS,
                "costable_ingredient_count": costable,
            }
        )
    return result


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
