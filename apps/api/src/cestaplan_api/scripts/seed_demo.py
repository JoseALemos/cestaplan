"""Deterministic, idempotent demo seed for CestaPlan.

Loads a FICTIONAL supermarket (``MercaEjemplo``) into the live database so the vertical
slice and the deterministic engine have a self-consistent catalogue and recipe book to
work with. Everything written here is synthetic (``is_synthetic=True`` /
``source_type='demo'``) and must never be presented as real.

Run::

    uv run python -m cestaplan_api.scripts.seed_demo

Idempotency strategy: *wipe-synthetic-then-insert*. Every run first deletes all synthetic
catalogue/recipe rows (and the demo ``DataSource``) in FK-safe order, then re-inserts the
whole dataset from :mod:`cestaplan_api.seed.data`. Running it repeatedly yields identical
row counts (no duplicate explosion). Determinism is fixed by a seeded ``random.Random``.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import (
    DataSource,
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductNutrition,
    ProductPrice,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    Retailer,
    Store,
)
from cestaplan_api.seed import data as seed_data

_RANDOM_SEED = 20260721
_UNIT_PRICE_Q = Decimal("0.000001")
_PRICE_TTL_DAYS = 30


def _d(value: object) -> Decimal:
    """Coerce a number/string into a Decimal without binary-float error."""
    return Decimal(str(value))


def _wipe_synthetic(session: Session) -> None:
    """Delete all synthetic demo rows in FK-safe order (idempotency)."""
    synthetic_products = select(Product.id).where(Product.is_synthetic.is_(True))
    # 1. mappings reference products + ingredients (no cascade) -> first.
    session.execute(
        delete(IngredientProductMapping).where(
            IngredientProductMapping.product_id.in_(synthetic_products)
        )
    )
    # 2. prices reference products (no cascade) -> before products.
    session.execute(delete(ProductPrice).where(ProductPrice.is_synthetic.is_(True)))
    # 3. recipes cascade to recipe_ingredient/recipe_step; delete before ingredients.
    session.execute(delete(Recipe).where(Recipe.is_synthetic.is_(True)))
    # 4. products cascade to product_nutrition.
    session.execute(delete(Product).where(Product.is_synthetic.is_(True)))
    # 5. ingredients (now unreferenced).
    session.execute(delete(Ingredient).where(Ingredient.is_synthetic.is_(True)))
    # 6. store / retailer.
    session.execute(delete(Store).where(Store.is_synthetic.is_(True)))
    session.execute(delete(Retailer).where(Retailer.is_synthetic.is_(True)))
    # 7. demo data source (identified by its stable slug).
    session.execute(delete(DataSource).where(DataSource.slug == seed_data.DATA_SOURCE_SLUG))
    session.flush()


def _seed(session: Session, rng: random.Random, now: datetime) -> dict[str, int]:
    observed_at = now - timedelta(days=1)
    expires_at = now + timedelta(days=_PRICE_TTL_DAYS)

    # --- data source ---
    source = DataSource(
        slug=seed_data.DATA_SOURCE_SLUG,
        name=seed_data.DATA_SOURCE_NAME,
        source_type="demo",
        adapter_key="demo",
        license_code="synthetic",
        attribution_text="Datos sintéticos de demostración de CestaPlan. No son reales.",
        is_enabled=True,
        url=None,
    )
    session.add(source)

    # --- retailer + store ---
    retailer = Retailer(
        slug=seed_data.RETAILER_SLUG,
        name=seed_data.RETAILER_NAME,
        adapter_key="demo",
        country="ES",
        is_active=True,
        is_synthetic=True,
    )
    session.add(retailer)
    session.flush()  # assign retailer.id

    store = Store(
        retailer_id=retailer.id,
        external_code=seed_data.STORE_EXTERNAL_CODE,
        name=seed_data.STORE_NAME,
        province=seed_data.STORE_PROVINCE,
        locality=seed_data.STORE_LOCALITY,
        postal_code=seed_data.STORE_POSTAL_CODE,
        latitude=_d("40.415363"),
        longitude=_d("-3.707398"),
        catalog_updated_at=observed_at,
        price_coverage_hint=_d("1.0"),
        is_active=True,
        is_synthetic=True,
    )
    session.add(store)
    session.flush()  # assign store.id

    counts = {
        "ingredient": 0,
        "product": 0,
        "product_price": 0,
        "product_nutrition": 0,
        "ingredient_product_mapping": 0,
        "recipe": 0,
        "recipe_ingredient": 0,
        "recipe_step": 0,
    }

    # --- ingredients, products, prices, nutrition, mappings ---
    ingredient_by_name: dict[str, Ingredient] = {}
    for spec in seed_data.INGREDIENTS:
        ingredient = Ingredient(
            canonical_name=spec["name"],
            display_name=spec["display"],
            category_code=spec["cat"],
            default_unit=spec["unit"],
            density_g_per_ml=_d(spec["density"]) if spec["density"] is not None else None,
            allergen_codes=list(spec["allergens"]) or None,
            is_synthetic=True,
        )
        session.add(ingredient)
        session.flush()  # assign ingredient.id
        ingredient_by_name[spec["name"]] = ingredient
        counts["ingredient"] += 1

        kcal, prot, carb, sug, fat, sat, fiber, salt = spec["nutr"]
        for pkg_index, (pkg_qty, amount_str) in enumerate(spec["packages"], start=1):
            brand = rng.choice(seed_data.BRANDS)
            qty = _d(pkg_qty)
            amount = _d(amount_str)
            unit_price = (amount / qty).quantize(_UNIT_PRICE_Q)
            external_id = f"DEMO-{spec['name']}-{pkg_index}".upper()
            size_label = _pack_label(pkg_qty, spec["unit"])

            product = Product(
                retailer_id=retailer.id,
                external_id=external_id,
                name=f"{spec['display']} {brand} {size_label}",
                brand=brand,
                package_quantity=qty,
                package_unit=spec["unit"],
                image_url=None,
                is_synthetic=True,
            )
            session.add(product)
            session.flush()  # assign product.id
            counts["product"] += 1

            session.add(
                ProductPrice(
                    retailer_id=retailer.id,
                    store_id=store.id,
                    product_id=product.id,
                    amount=amount,
                    currency="EUR",
                    package_quantity=qty,
                    package_unit=spec["unit"],
                    unit_price=unit_price,
                    promotion=None,
                    availability="in_stock",
                    source_type="demo",
                    source_name=seed_data.SOURCE_NAME,
                    source_url=None,
                    observed_at=observed_at,
                    imported_at=now,
                    expires_at=expires_at,
                    confidence_score=_d("1.0"),
                    import_id=None,
                    verification_status="machine_verified",
                    is_synthetic=True,
                )
            )
            counts["product_price"] += 1

            session.add(
                ProductNutrition(
                    product_id=product.id,
                    basis_quantity=_d(100),
                    basis_unit="ml" if spec["unit"] == "ml" else "g",
                    energy_kcal=_d(kcal),
                    protein_g=_d(prot),
                    carbohydrate_g=_d(carb),
                    sugars_g=_d(sug),
                    fat_g=_d(fat),
                    saturated_fat_g=_d(sat),
                    fiber_g=_d(fiber),
                    salt_g=_d(salt),
                    allergens=list(spec["allergens"]) or None,
                    traces=list(spec["traces"]) or None,
                    ingredients_text=None,
                    source_type="demo",
                    source_url=None,
                    is_synthetic=True,
                )
            )
            counts["product_nutrition"] += 1

            session.add(
                IngredientProductMapping(
                    ingredient_id=ingredient.id,
                    product_id=product.id,
                    retailer_id=retailer.id,
                    conversion_factor=_d(1),
                    preference_rank=pkg_index,
                    confidence_score=_d("0.9"),
                    is_active=True,
                )
            )
            counts["ingredient_product_mapping"] += 1

    # --- recipes ---
    for rspec in seed_data.RECIPES:
        recipe = Recipe(
            household_id=None,
            origin="seed",
            is_public=True,
            is_synthetic=True,
            title=rspec["title"],
            description=rspec["description"],
            servings=rspec["servings"],
            meal_types=list(rspec["meal_types"]),
            cuisine=rspec["cuisine"],
            preference_tags=list(rspec["tags"]),
            preparation_minutes=rspec["prep"],
            cooking_minutes=rspec["cook"],
            required_equipment=list(rspec["equipment"]) or None,
            leftover_reuse=None,
            storage_instructions=None,
            reheating_instructions=None,
            generated_by=None,
        )
        session.add(recipe)
        session.flush()  # assign recipe.id
        counts["recipe"] += 1

        for canonical_name, quantity, optional, subgroup in rspec["ingredients"]:
            ingredient = ingredient_by_name[canonical_name]
            session.add(
                RecipeIngredient(
                    recipe_id=recipe.id,
                    ingredient_id=ingredient.id,
                    canonical_name=canonical_name,
                    display_name=ingredient.display_name,
                    quantity=_d(quantity),
                    unit=ingredient.default_unit or "g",
                    optional=optional,
                    substitution_group=subgroup,
                )
            )
            counts["recipe_ingredient"] += 1

        for step_number, instruction in enumerate(rspec["steps"], start=1):
            session.add(
                RecipeStep(
                    recipe_id=recipe.id,
                    step_number=step_number,
                    instruction=instruction,
                    duration_minutes=None,
                )
            )
            counts["recipe_step"] += 1

    return counts


def _pack_label(quantity: float, unit: str) -> str:
    """Human-readable package label, e.g. ``500 g``, ``1 kg``, ``6 ud``."""
    if unit == "unit":
        return f"{int(quantity)} ud"
    if unit == "g" and quantity >= 1000 and quantity % 1000 == 0:
        return f"{int(quantity // 1000)} kg"
    if unit == "ml" and quantity >= 1000 and quantity % 1000 == 0:
        return f"{int(quantity // 1000)} L"
    return f"{int(quantity)} {unit}"


def _validate_before_insert() -> None:
    """Fail fast if any recipe references a canonical name absent from the catalogue."""
    known = {spec["name"] for spec in seed_data.INGREDIENTS}
    missing: set[str] = set()
    for rspec in seed_data.RECIPES:
        for canonical_name, *_ in rspec["ingredients"]:
            if canonical_name not in known:
                missing.add(canonical_name)
    if missing:
        raise ValueError(
            "Recipe ingredients without a catalogue ingredient (unsatisfiable): "
            + ", ".join(sorted(missing))
        )


def _verify(session: Session) -> tuple[dict[str, int], list[str]]:
    """Query live counts and find recipe ingredients with no priced product."""
    live = {
        "ingredient": session.scalar(
            select(func.count()).select_from(Ingredient).where(Ingredient.is_synthetic.is_(True))
        ),
        "product": session.scalar(
            select(func.count()).select_from(Product).where(Product.is_synthetic.is_(True))
        ),
        "product_price": session.scalar(
            select(func.count())
            .select_from(ProductPrice)
            .where(ProductPrice.is_synthetic.is_(True))
        ),
        "product_nutrition": session.scalar(
            select(func.count())
            .select_from(ProductNutrition)
            .where(ProductNutrition.is_synthetic.is_(True))
        ),
        "ingredient_product_mapping": session.scalar(
            select(func.count()).select_from(IngredientProductMapping)
        ),
        "recipe": session.scalar(
            select(func.count()).select_from(Recipe).where(Recipe.is_synthetic.is_(True))
        ),
        "recipe_ingredient": session.scalar(select(func.count()).select_from(RecipeIngredient)),
        "recipe_step": session.scalar(select(func.count()).select_from(RecipeStep)),
    }

    # Ingredient ids that resolve to at least one product carrying a (synthetic) price.
    priced_ingredients = set(
        session.scalars(
            select(IngredientProductMapping.ingredient_id)
            .join(ProductPrice, ProductPrice.product_id == IngredientProductMapping.product_id)
            .distinct()
        ).all()
    )
    orphans: list[str] = []
    rows = session.execute(
        select(RecipeIngredient.canonical_name, RecipeIngredient.ingredient_id)
        .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
        .where(Recipe.is_synthetic.is_(True))
    ).all()
    for canonical_name, ingredient_id in rows:
        if ingredient_id not in priced_ingredients:
            orphans.append(canonical_name)
    # func.count() is typed int | None by SQLAlchemy but never returns NULL; coerce to int.
    return {key: (value or 0) for key, value in live.items()}, sorted(set(orphans))


def main() -> None:
    _validate_before_insert()
    rng = random.Random(_RANDOM_SEED)
    now = datetime.now(UTC)

    with SessionLocal() as session:
        _wipe_synthetic(session)
        counts = _seed(session, rng, now)
        session.commit()
        live, orphans = _verify(session)

    print("CestaPlan demo seed — MercaEjemplo (synthetic, is_synthetic=True)")
    print(f"  retailer : {seed_data.RETAILER_NAME} ({seed_data.RETAILER_SLUG})")
    print(
        f"  store    : {seed_data.STORE_NAME} — {seed_data.STORE_LOCALITY} "
        f"({seed_data.STORE_POSTAL_CODE}), cod. {seed_data.STORE_EXTERNAL_CODE}"
    )
    print("  inserted this run:")
    for key in sorted(counts):
        print(f"    {key:28s} {counts[key]:>5d}")
    print("  live synthetic row counts:")
    for key in sorted(live):
        print(f"    {key:28s} {live[key]:>5d}")
    if orphans:
        print(f"  ORPHAN recipe ingredients (no priced product): {orphans}")
        raise SystemExit(1)
    print("  OK: every recipe ingredient maps to a priced catalogue product.")


if __name__ == "__main__":
    main()
