"""Recipe-catalog coverage for a provider/store/scope (spec §Z).

The headline metric is NOT "how many products were imported" but "what fraction of recipes can
be COSTED correctly with the data available". A recipe is fully costable only when every
mandatory ingredient is mapped, has an eligible product, a usable price, a resolvable costing
mode (fixed package / variable weight / variable volume / discrete unit), a compatible price
scope, no staging data used in a production query, and no broken constraint.

Pure evaluation over the DB; writes nothing. A ten-product sample yields a low, honest coverage.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, fields
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
    Ingredient,
    IngredientProductMapping,
    ProductVariant,
    Recipe,
    RecipeIngredient,
    Retailer,
)

# Scopes acceptable when a specific location is requested (more specific than the requirement).
_SCOPE_RANK = {
    "exact_store": 1,
    "delivery_zone": 2,
    "postal_code": 3,
    "municipality": 4,
    "province": 5,
    "region": 6,
    "national": 7,
    "unknown": 8,
}


def _ratio(n: int, total: int) -> Decimal:
    return Decimal("0") if total <= 0 else (Decimal(n) / Decimal(total)).quantize(Decimal("0.0001"))


@dataclass(slots=True)
class IngredientStatus:
    ingredient_id: int
    canonical_name: str
    category_code: str | None
    mapped: bool = False
    has_valid_price: bool = False
    package_resolved: bool = False
    fresh: bool = False
    scope_ok: bool = False
    costing_mode: ProductCostingMode = ProductCostingMode.UNRESOLVED
    selected_product_id: int | None = None

    @property
    def costable(self) -> bool:
        return (
            self.mapped
            and self.has_valid_price
            and self.package_resolved
            and self.scope_ok
            and self.costing_mode is not ProductCostingMode.UNRESOLVED
        )


@dataclass(slots=True)
class RecipeCatalogCoverage:
    provider_code: str
    retailer_slug: str
    price_scope: str
    evaluated_at: str
    store_id: int | None = None
    total_recipes: int = 0
    fully_costable_recipes: int = 0
    partially_costable_recipes: int = 0
    uncostable_recipes: int = 0
    total_recipe_ingredients: int = 0
    mapped_ingredients: int = 0
    unmapped_ingredients: int = 0
    ingredients_with_valid_price: int = 0
    ingredients_without_price: int = 0
    ingredients_with_complete_package: int = 0
    ingredients_with_unresolved_package: int = 0
    ingredients_with_fresh_price: int = 0
    ingredients_with_stale_price: int = 0
    ingredient_mapping_coverage: Decimal = Decimal("0")
    price_coverage: Decimal = Decimal("0")
    package_coverage: Decimal = Decimal("0")
    costing_coverage: Decimal = Decimal("0")
    coverage_by_meal_type: dict[str, str] = field(default_factory=dict)
    coverage_by_diet: dict[str, str] = field(default_factory=dict)
    coverage_by_preference: dict[str, str] = field(default_factory=dict)
    coverage_by_category: dict[str, str] = field(default_factory=dict)
    priority_unmapped_ingredients: list[dict[str, object]] = field(default_factory=list)
    deficit_categories: list[dict[str, object]] = field(default_factory=list)
    incompatible_scope_ingredients: int = 0
    partially_costable_detail: list[dict[str, object]] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, Decimal) else value
        return out


_KNOWN_DIETS = {"vegetariano", "vegano", "sin_gluten", "sin_lactosa", "keto", "paleo"}


def evaluate_recipe_catalog_coverage(
    db: Session,
    provider_code: str,
    *,
    scope: str = "staging",
    store_id: int | None = None,
    postal_code: str | None = None,
    meal_type: str | None = None,
    diet: str | None = None,
    recipe_limit: int | None = None,
    now: datetime | None = None,
) -> RecipeCatalogCoverage:
    """Evaluate how much of the (synthetic) recipe set is costable with ``provider_code`` data."""
    now = now or datetime.now(UTC)
    staging = scope == "staging"
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    required_scope = "exact_store" if store_id else ("postal_code" if postal_code else "national")
    cov = RecipeCatalogCoverage(
        provider_code=provider_code,
        retailer_slug=retailer_slug,
        price_scope=required_scope,
        evaluated_at=now.isoformat(),
        store_id=store_id,
    )

    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()

    # Batch-load the retailer's active ingredient mappings and priced variants.
    ing_to_products: dict[int, list[int]] = {}
    variants_by_product: dict[int, list[ProductVariant]] = {}
    if retailer_id is not None:
        rows = db.execute(
            select(IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id)
            .where(
                IngredientProductMapping.retailer_id == retailer_id,
                IngredientProductMapping.is_active.is_(True),
            )
            .order_by(IngredientProductMapping.preference_rank.nulls_last())
        ).all()
        for ing_id, prod_id in rows:
            if ing_id is None or prod_id is None:
                continue
            ing_to_products.setdefault(ing_id, []).append(prod_id)
        for variant in db.execute(
            select(ProductVariant).where(
                ProductVariant.retailer_id == retailer_id, ProductVariant.active.is_(True)
            )
        ).scalars():
            if variant.product_id is not None:
                variants_by_product.setdefault(variant.product_id, []).append(variant)

    recipes = _load_recipes(db, meal_type=meal_type, diet=diet, limit=recipe_limit)
    ing_meta = _ingredient_meta(db)
    prices = CurrentPriceService()
    unmapped_counter: Counter[str] = Counter()
    category_totals: Counter[str] = Counter()
    category_costable: Counter[str] = Counter()
    meal_totals: Counter[str] = Counter()
    meal_full: Counter[str] = Counter()
    pref_totals: Counter[str] = Counter()
    pref_full: Counter[str] = Counter()

    for recipe in recipes:
        cov.total_recipes += 1
        mandatory = [ri for ri in recipe.ingredients if not ri.optional]
        statuses = [
            _evaluate_ingredient(
                db,
                ri,
                ing_meta,
                ing_to_products,
                variants_by_product,
                prices,
                staging=staging,
                store_id=store_id,
                required_scope=required_scope,
                now=now,
            )
            for ri in mandatory
        ]
        cov.total_recipe_ingredients += len(statuses)
        missing: list[str] = []
        for st in statuses:
            cov.mapped_ingredients += st.mapped
            cov.unmapped_ingredients += not st.mapped
            cov.ingredients_with_valid_price += st.has_valid_price
            cov.ingredients_without_price += not st.has_valid_price
            cov.ingredients_with_complete_package += st.package_resolved
            cov.ingredients_with_unresolved_package += not st.package_resolved
            cov.ingredients_with_fresh_price += st.fresh
            cov.ingredients_with_stale_price += st.has_valid_price and not st.fresh
            cov.incompatible_scope_ingredients += st.has_valid_price and not st.scope_ok
            cat = st.category_code or "sin_categoria"
            category_totals[cat] += 1
            if st.costable:
                category_costable[cat] += 1
            else:
                if not st.mapped:
                    unmapped_counter[st.canonical_name] += 1
                missing.append(_missing_reason(st))

        costable_ct = sum(1 for st in statuses if st.costable)
        full = bool(statuses) and costable_ct == len(statuses)
        for mt in recipe.meal_types or ["sin_tipo"]:
            meal_totals[mt] += 1
            meal_full[mt] += full
        for tag in recipe.preference_tags or []:
            pref_totals[tag] += 1
            pref_full[tag] += full
        if full:
            cov.fully_costable_recipes += 1
        elif costable_ct > 0:
            cov.partially_costable_recipes += 1
            cov.partially_costable_detail.append(
                {"recipe_id": recipe.id, "title": recipe.title, "missing": missing[:8]}
            )
        else:
            cov.uncostable_recipes += 1

    _finalise(
        cov,
        unmapped_counter,
        category_totals,
        category_costable,
        meal_totals,
        meal_full,
        pref_totals,
        pref_full,
    )
    return cov


def _evaluate_ingredient(
    db: Session,
    ri: RecipeIngredient,
    ing_meta: dict[int, tuple[str | None]],
    ing_to_products: dict[int, list[int]],
    variants_by_product: dict[int, list[ProductVariant]],
    prices: CurrentPriceService,
    *,
    staging: bool,
    store_id: int | None,
    required_scope: str,
    now: datetime,
) -> IngredientStatus:
    (category,) = ing_meta.get(ri.ingredient_id, (None,))
    st = IngredientStatus(ri.ingredient_id, ri.canonical_name, category)
    product_ids = ing_to_products.get(ri.ingredient_id, [])
    st.mapped = bool(product_ids)
    if not st.mapped:
        return st
    for product_id in product_ids:
        for variant in variants_by_product.get(product_id, []):
            price = prices.current(db, variant.id, store_id=store_id, as_of=now, staging=staging)
            if price is None:
                continue
            mode = classify_variant_costing_mode(
                sell_unit=variant.sell_unit,
                variable_weight=variant.variable_weight,
                net_content_quantity=variant.net_content_quantity,
                net_content_unit=variant.net_content_unit,
                unit_price=variant.unit_price,
                unit_price_unit=variant.unit_price_unit,
                has_price=True,
            )
            scope_ok = _scope_ok(price.price_scope, required_scope)
            fresh = price.status is FreshnessStatus.FRESH
            better = mode is not ProductCostingMode.UNRESOLVED and scope_ok and fresh
            if better or not st.has_valid_price:
                st.has_valid_price = True
                st.selected_product_id = product_id
                st.package_resolved = mode is not ProductCostingMode.UNRESOLVED
                st.costing_mode = mode
                st.scope_ok = scope_ok
                st.fresh = fresh
            if better:
                return st
    return st


def _scope_ok(candidate_scope: str, required_scope: str) -> bool:
    if required_scope == "national":
        return True  # any real scope satisfies a national requirement
    if candidate_scope == "unknown":
        return False
    return _SCOPE_RANK.get(candidate_scope, 99) <= _SCOPE_RANK.get(required_scope, 0)


def _missing_reason(st: IngredientStatus) -> str:
    if not st.mapped:
        return f"{st.canonical_name}: sin mapear"
    if not st.has_valid_price:
        return f"{st.canonical_name}: sin precio"
    if not st.scope_ok:
        return f"{st.canonical_name}: ámbito incompatible"
    if st.costing_mode is ProductCostingMode.UNRESOLVED:
        return f"{st.canonical_name}: envase no resoluble"
    if not st.fresh:
        return f"{st.canonical_name}: precio antiguo"
    return f"{st.canonical_name}: no calculable"


def _load_recipes(
    db: Session, *, meal_type: str | None, diet: str | None, limit: int | None
) -> list[Recipe]:
    stmt = (
        select(Recipe)
        .where(Recipe.deleted_at.is_(None), Recipe.is_synthetic.is_(True))
        .order_by(Recipe.id)
    )
    recipes = list(db.execute(stmt).scalars())
    if meal_type:
        recipes = [r for r in recipes if r.meal_types and meal_type in r.meal_types]
    if diet:
        recipes = [r for r in recipes if r.preference_tags and diet in r.preference_tags]
    return recipes[:limit] if limit else recipes


def _ingredient_meta(db: Session) -> dict[int, tuple[str | None]]:
    return {
        row.id: (row.category_code,)
        for row in db.execute(select(Ingredient.id, Ingredient.category_code)).all()
    }


def _finalise(
    cov: RecipeCatalogCoverage,
    unmapped: Counter[str],
    cat_totals: Counter[str],
    cat_costable: Counter[str],
    meal_totals: Counter[str],
    meal_full: Counter[str],
    pref_totals: Counter[str],
    pref_full: Counter[str],
) -> None:
    total_ing = cov.total_recipe_ingredients
    cov.ingredient_mapping_coverage = _ratio(cov.mapped_ingredients, total_ing)
    cov.price_coverage = _ratio(cov.ingredients_with_valid_price, total_ing)
    cov.package_coverage = _ratio(cov.ingredients_with_complete_package, total_ing)
    # Costing coverage: ingredients that clear EVERY gate (mapped+price+package+scope+fresh).
    costable = sum(cat_costable.values())
    cov.costing_coverage = _ratio(costable, total_ing)
    cov.coverage_by_meal_type = {
        mt: str(_ratio(meal_full[mt], meal_totals[mt])) for mt in sorted(meal_totals)
    }
    cov.coverage_by_preference = {
        t: str(_ratio(pref_full[t], pref_totals[t])) for t in sorted(pref_totals)
    }
    cov.coverage_by_diet = {
        t: cov.coverage_by_preference[t] for t in cov.coverage_by_preference if t in _KNOWN_DIETS
    }
    cov.coverage_by_category = {
        c: str(_ratio(cat_costable[c], cat_totals[c])) for c in sorted(cat_totals)
    }
    cov.priority_unmapped_ingredients = [
        {"canonical_name": name, "recipes_blocked": count}
        for name, count in unmapped.most_common(15)
    ]
    deficit: list[dict[str, object]] = [
        {"category": c, "costable_coverage": str(_ratio(cat_costable[c], cat_totals[c]))}
        for c in sorted(cat_totals, key=lambda c: _ratio(cat_costable[c], cat_totals[c]))
        if _ratio(cat_costable[c], cat_totals[c]) < Decimal("0.5")
    ]
    cov.deficit_categories = deficit[:10]
    cov.next_steps = _next_steps(cov)


def _next_steps(cov: RecipeCatalogCoverage) -> list[str]:
    steps: list[str] = []
    if cov.priority_unmapped_ingredients:
        top = ", ".join(str(i["canonical_name"]) for i in cov.priority_unmapped_ingredients[:5])
        steps.append(f"Mapear ingredientes prioritarios: {top}")
    if cov.ingredients_without_price and cov.mapped_ingredients:
        steps.append("Capturar precios para los productos mapeados sin precio utilizable.")
    if cov.ingredients_with_unresolved_package:
        steps.append("Obtener contenido de envase (cantidad+unidad) o venta a peso confirmada.")
    if cov.incompatible_scope_ingredients:
        steps.append("Resolver el ámbito geográfico (tienda/código postal) de los precios.")
    if not steps:
        steps.append("Cobertura suficiente para evaluación; validar en modo sombra.")
    return steps


__all__ = ["IngredientStatus", "RecipeCatalogCoverage", "evaluate_recipe_catalog_coverage"]
