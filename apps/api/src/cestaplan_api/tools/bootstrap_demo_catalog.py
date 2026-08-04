"""Additive, idempotent production bootstrap of the synthetic ``MercaEjemplo`` demo catalogue.

Unlike :mod:`cestaplan_api.scripts.seed_demo` (a *wipe-synthetic-then-insert* dev tool), this
tool NEVER deletes, truncates or reclassifies any existing row. It consumes the SAME canonical
dataset (:mod:`cestaplan_api.seed.data`) and *get-or-creates* every demo row by a stable natural
key, so it can run safely on a production database that already holds unrelated (non-synthetic)
recipes, ingredients and products — and a second ``--apply`` is a no-op (``created=0``).

Guarantees:
  * additive only — issues NO ``DELETE``/``TRUNCATE``/``DROP`` and no ``UPDATE`` of existing
    catalogue rows (the demo ``ProviderActivation`` is the ONLY row ever updated, and only under
    ``--activate-demo``);
  * never changes ``is_synthetic`` of an existing row, and never converts a real row into demo;
  * fails closed if a demo natural key is already owned by a NON-synthetic row;
  * reuses an existing ingredient by ``canonical_name`` (the 17 that currently collide are REUSED,
    never duplicated) and only creates the demo ingredients that are absent;
  * every product / price / mapping it creates belongs exclusively to the synthetic MercaEjemplo
    retailer, is ``is_synthetic=True`` and ``source_type='demo'``, and never touches a real chain;
  * makes NO external/network calls and needs no credentials;
  * idempotent by natural-key existence checks — never by delete-and-recreate.

Modes::

    python -m cestaplan_api.tools.bootstrap_demo_catalog --dry-run   [--no-activate-demo]
    python -m cestaplan_api.tools.bootstrap_demo_catalog --apply --activate-demo

``--dry-run`` runs the full logic inside a transaction that ALWAYS rolls back and prints a
sanitized per-model diff. ``--apply`` commits a single transaction after an internal readiness
check; any error rolls the whole transaction back.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import (
    DataSource,
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductNutrition,
    ProductPrice,
    ProviderActivation,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    Retailer,
    Store,
)
from cestaplan_api.scripts.seed_demo import _pack_label
from cestaplan_api.seed import data as seed_data
from cestaplan_api.services.catalog_readiness import catalog_readiness_report

# The canonical demo provider identity (rights already seeded by bootstrap_source_rights).
DEMO_PROVIDER_CODE = "demo"
_RANDOM_SEED = 20260721  # SAME seed as seed_demo, so product brand names are byte-stable.
_UNIT_PRICE_Q = Decimal("0.000001")
_PRICE_TTL_DAYS = 30
# The canonical base unit each ingredient is sold/measured in per the demo dataset (g|ml|unit).
# A demo recipe MUST express its quantities in this unit — matching the demo product's package
# unit and the conversion_factor=1 mapping — so costing resolves. When an ingredient is REUSED
# from a pre-existing row whose declared ``default_unit`` differs (e.g. a human-friendly
# "unidad"/"cucharadita"), the recipe must still use the dataset base unit, not that default_unit,
# or the line becomes uncostable (recipe-unit vs product-unit mismatch, no conversion).
_UNIT_BY_NAME: dict[str, str] = {spec["name"]: spec["unit"] for spec in seed_data.INGREDIENTS}


def _recipe_unit(canonical_name: str, ingredient: Ingredient) -> str:
    return _UNIT_BY_NAME.get(canonical_name) or ingredient.default_unit or "g"


class DemoBootstrapError(RuntimeError):
    """A fail-closed bootstrap failure carrying a stable, sanitized ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(slots=True)
class ModelDiff:
    created: int = 0
    reused: int = 0
    updated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"created": self.created, "reused": self.reused, "updated": self.updated}


@dataclass(slots=True)
class BootstrapDiff:
    models: dict[str, ModelDiff] = field(default_factory=dict)
    reused_ingredient_names: list[str] = field(default_factory=list)
    created_ingredient_names: list[str] = field(default_factory=list)
    recipe_title_collisions_with_non_synthetic: list[str] = field(default_factory=list)
    activated_demo: bool = False
    deletes: int = 0  # ALWAYS 0 — this tool never deletes; surfaced for auditability.

    def m(self, name: str) -> ModelDiff:
        return self.models.setdefault(name, ModelDiff())

    def as_dict(self) -> dict[str, Any]:
        return {
            "models": {k: v.as_dict() for k, v in sorted(self.models.items())},
            "reused_ingredient_count": len(self.reused_ingredient_names),
            "created_ingredient_count": len(self.created_ingredient_names),
            "reused_ingredient_names": sorted(self.reused_ingredient_names),
            "created_ingredient_names": sorted(self.created_ingredient_names),
            "recipe_title_collisions_with_non_synthetic": sorted(
                self.recipe_title_collisions_with_non_synthetic
            ),
            "activated_demo": self.activated_demo,
            "deletes": self.deletes,
        }


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _brand_matrix(rng: random.Random) -> list[list[str]]:
    """Precompute the brand for every (ingredient, package) as a pure function of position, using a
    fresh seeded RNG. Making brands position-deterministic (not create-order-dependent) keeps the
    catalogue byte-stable regardless of what already exists in the DB."""
    return [
        [rng.choice(seed_data.BRANDS) for _ in spec["packages"]]
        for spec in seed_data.INGREDIENTS
    ]


def _get_or_fail_ingredient(session: Session, canonical_name: str) -> Ingredient | None:
    rows = session.execute(
        select(Ingredient).where(Ingredient.canonical_name == canonical_name)
    ).scalars().all()
    if len(rows) > 1:
        raise DemoBootstrapError("ingredient_identity_ambiguous", canonical_name)
    return rows[0] if rows else None


def _demo_retailer(session: Session, diff: BootstrapDiff) -> Retailer:
    row = session.execute(
        select(Retailer).where(Retailer.slug == seed_data.RETAILER_SLUG)
    ).scalar_one_or_none()
    if row is not None:
        if not row.is_synthetic:
            raise DemoBootstrapError(
                "demo_natural_key_owned_by_non_synthetic_row", f"retailer:{seed_data.RETAILER_SLUG}"
            )
        diff.m("retailer").reused += 1
        return row
    row = Retailer(
        slug=seed_data.RETAILER_SLUG, name=seed_data.RETAILER_NAME, adapter_key="demo",
        country="ES", is_active=True, is_synthetic=True,
    )
    session.add(row)
    session.flush()
    diff.m("retailer").created += 1
    return row


def _demo_data_source(session: Session, diff: BootstrapDiff) -> None:
    row = session.execute(
        select(DataSource).where(DataSource.slug == seed_data.DATA_SOURCE_SLUG)
    ).scalar_one_or_none()
    if row is not None:
        if row.source_type != "demo":
            raise DemoBootstrapError(
                "demo_natural_key_owned_by_non_synthetic_row",
                f"data_source:{seed_data.DATA_SOURCE_SLUG}",
            )
        diff.m("data_source").reused += 1
        return
    session.add(DataSource(
        slug=seed_data.DATA_SOURCE_SLUG, name=seed_data.DATA_SOURCE_NAME, source_type="demo",
        adapter_key="demo", license_code="synthetic",
        attribution_text="Datos sintéticos de demostración de CestaPlan. No son reales.",
        is_enabled=True, url=None,
    ))
    diff.m("data_source").created += 1


def _demo_store(session: Session, retailer_id: int, *, external_code: str, name: str,
                province: str, locality: str, postal_code: str, lat: str, lon: str,
                observed_at: datetime, diff: BootstrapDiff) -> Store:
    row = session.execute(
        select(Store).where(Store.retailer_id == retailer_id, Store.external_code == external_code)
    ).scalar_one_or_none()
    if row is not None:
        if not row.is_synthetic:
            raise DemoBootstrapError(
                "demo_natural_key_owned_by_non_synthetic_row", f"store:{external_code}"
            )
        diff.m("store").reused += 1
        return row
    row = Store(
        retailer_id=retailer_id, external_code=external_code, name=name, province=province,
        locality=locality, postal_code=postal_code, latitude=_d(lat), longitude=_d(lon),
        catalog_updated_at=observed_at, price_coverage_hint=_d("1.0"), is_active=True,
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    diff.m("store").created += 1
    return row


def bootstrap(session: Session, *, activate: bool) -> BootstrapDiff:
    """Get-or-create the whole demo catalogue additively. Caller controls commit/rollback."""
    diff = BootstrapDiff()
    now = datetime.now(UTC)
    observed_at = now - timedelta(days=1)
    expires_at = now + timedelta(days=_PRICE_TTL_DAYS)
    brands = _brand_matrix(random.Random(_RANDOM_SEED))

    retailer = _demo_retailer(session, diff)
    _demo_data_source(session, diff)
    store = _demo_store(
        session, retailer.id, external_code=seed_data.STORE_EXTERNAL_CODE,
        name=seed_data.STORE_NAME, province=seed_data.STORE_PROVINCE,
        locality=seed_data.STORE_LOCALITY, postal_code=seed_data.STORE_POSTAL_CODE,
        lat="40.415363", lon="-3.707398", observed_at=observed_at, diff=diff)
    store2 = _demo_store(
        session, retailer.id, external_code=seed_data.STORE2_EXTERNAL_CODE,
        name=seed_data.STORE2_NAME, province=seed_data.STORE2_PROVINCE,
        locality=seed_data.STORE2_LOCALITY, postal_code=seed_data.STORE2_POSTAL_CODE,
        lat="40.541400", lon="-3.641800", observed_at=observed_at, diff=diff)
    store2_factor = _d(seed_data.STORE2_PRICE_FACTOR)

    # --- ingredients (REUSE by canonical_name; create only the absent demo ones) ---
    ingredient_by_name: dict[str, Ingredient] = {}
    for spec in seed_data.INGREDIENTS:
        existing = _get_or_fail_ingredient(session, spec["name"])
        if existing is not None:
            ingredient_by_name[spec["name"]] = existing
            diff.m("ingredient").reused += 1
            diff.reused_ingredient_names.append(spec["name"])
            continue
        kcal, prot, carb, sug, fat, sat, fiber, salt = spec["nutr"]
        ing = Ingredient(
            canonical_name=spec["name"], display_name=spec["display"],
            category_code=spec["cat"], default_unit=spec["unit"],
            density_g_per_ml=_d(spec["density"]) if spec["density"] is not None else None,
            allergen_codes=list(spec["allergens"]) or None, is_synthetic=True,
        )
        session.add(ing)
        session.flush()
        ingredient_by_name[spec["name"]] = ing
        diff.m("ingredient").created += 1
        diff.created_ingredient_names.append(spec["name"])

    # --- products, prices, nutrition, mappings (all MercaEjemplo-scoped, synthetic) ---
    for ing_index, spec in enumerate(seed_data.INGREDIENTS):
        ingredient = ingredient_by_name[spec["name"]]
        kcal, prot, carb, sug, fat, sat, fiber, salt = spec["nutr"]
        for pkg_index, (pkg_qty, amount_str) in enumerate(spec["packages"], start=1):
            brand = brands[ing_index][pkg_index - 1]
            qty = _d(pkg_qty)
            amount = _d(amount_str)
            unit_price = (amount / qty).quantize(_UNIT_PRICE_Q)
            external_id = f"DEMO-{spec['name']}-{pkg_index}".upper()
            size_label = _pack_label(pkg_qty, spec["unit"])

            product = session.execute(
                select(Product).where(
                    Product.retailer_id == retailer.id, Product.external_id == external_id)
            ).scalar_one_or_none()
            if product is None:
                product = Product(
                    retailer_id=retailer.id, external_id=external_id,
                    name=f"{spec['display']} {brand} {size_label}", brand=brand,
                    package_quantity=qty, package_unit=spec["unit"], image_url=None,
                    is_synthetic=True,
                )
                session.add(product)
                session.flush()
                diff.m("product").created += 1
            else:
                if not product.is_synthetic:
                    raise DemoBootstrapError(
                        "demo_natural_key_owned_by_non_synthetic_row", f"product:{external_id}")
                diff.m("product").reused += 1

            store2_amount = (amount * store2_factor).quantize(_d("0.01"))
            store2_unit_price = (store2_amount / qty).quantize(_UNIT_PRICE_Q)
            for price_store_id, price_amount, price_unit in (
                (store.id, amount, unit_price),
                (store2.id, store2_amount, store2_unit_price),
            ):
                # Idempotent by (retailer, store, product) existence — ProductPrice has no natural
                # unique key, so we never blindly append a second row on re-run.
                exists = session.execute(
                    select(func.count()).select_from(ProductPrice).where(
                        ProductPrice.retailer_id == retailer.id,
                        ProductPrice.store_id == price_store_id,
                        ProductPrice.product_id == product.id,
                    )
                ).scalar_one()
                if exists:
                    diff.m("product_price").reused += 1
                    continue
                if not price_amount > 0:  # defence-in-depth: demo prices are strictly positive
                    raise DemoBootstrapError("demo_price_not_positive", external_id)
                session.add(ProductPrice(
                    retailer_id=retailer.id, store_id=price_store_id, product_id=product.id,
                    amount=price_amount, currency="EUR", package_quantity=qty,
                    package_unit=spec["unit"], unit_price=price_unit, promotion=None,
                    availability="in_stock", source_type="demo",
                    source_name=seed_data.SOURCE_NAME, source_url=None, observed_at=observed_at,
                    imported_at=now, expires_at=expires_at,
                    confidence_score=_d("1.0"), import_id=None,
                    verification_status="machine_verified", is_synthetic=True,
                ))
                diff.m("product_price").created += 1

            nutri_exists = session.execute(
                select(func.count()).select_from(ProductNutrition).where(
                    ProductNutrition.product_id == product.id)
            ).scalar_one()
            if nutri_exists:
                diff.m("product_nutrition").reused += 1
            else:
                session.add(ProductNutrition(
                    product_id=product.id, basis_quantity=_d(100),
                    basis_unit="ml" if spec["unit"] == "ml" else "g", energy_kcal=_d(kcal),
                    protein_g=_d(prot), carbohydrate_g=_d(carb), sugars_g=_d(sug), fat_g=_d(fat),
                    saturated_fat_g=_d(sat), fiber_g=_d(fiber), salt_g=_d(salt),
                    allergens=list(spec["allergens"]) or None, traces=list(spec["traces"]) or None,
                    ingredients_text=None, source_type="demo", source_url=None, is_synthetic=True,
                ))
                diff.m("product_nutrition").created += 1

            mapping_exists = session.execute(
                select(func.count()).select_from(IngredientProductMapping).where(
                    IngredientProductMapping.ingredient_id == ingredient.id,
                    IngredientProductMapping.product_id == product.id)
            ).scalar_one()
            if mapping_exists:
                diff.m("ingredient_product_mapping").reused += 1
            else:
                session.add(IngredientProductMapping(
                    ingredient_id=ingredient.id, product_id=product.id, retailer_id=retailer.id,
                    conversion_factor=_d(1), preference_rank=pkg_index, confidence_score=_d("0.9"),
                    verification_status="human_verified", is_active=True,
                ))
                diff.m("ingredient_product_mapping").created += 1

    # --- demo recipes (identity = is_synthetic=True + title; create only when absent) ---
    for rspec in seed_data.RECIPES:
        title = rspec["title"]
        # Note a title shared with a NON-synthetic recipe (left untouched; identity stays the
        # is_synthetic flag — the two are always distinguishable, never merged or overwritten).
        nonsynthetic_same_title = session.execute(
            select(func.count()).select_from(Recipe).where(
                Recipe.title == title, Recipe.is_synthetic.is_(False))
        ).scalar_one()
        if nonsynthetic_same_title:
            diff.recipe_title_collisions_with_non_synthetic.append(title)
        existing = session.execute(
            select(Recipe).where(Recipe.title == title, Recipe.is_synthetic.is_(True))
        ).scalars().all()
        if len(existing) > 1:
            raise DemoBootstrapError("demo_recipe_identity_ambiguous", title)
        if existing:
            # Self-heal: an earlier bootstrap may have stored a reused ingredient's declared
            # default_unit (e.g. "unidad") instead of the dataset base unit, leaving the line
            # uncostable. Correct ONLY the unit of this demo recipe's own ingredient rows; never
            # touch a non-synthetic recipe. Idempotent (a no-op once units are correct).
            for ri in session.execute(
                select(RecipeIngredient).where(
                    RecipeIngredient.recipe_id == existing[0].id)
            ).scalars():
                want = _UNIT_BY_NAME.get(ri.canonical_name)
                if want and ri.unit != want:
                    ri.unit = want
                    diff.m("recipe_ingredient").updated += 1
            diff.m("recipe").reused += 1
            continue
        recipe = Recipe(
            household_id=None, origin="seed", is_public=True, is_synthetic=True, title=title,
            description=rspec["description"], servings=rspec["servings"],
            meal_types=list(rspec["meal_types"]), cuisine=rspec["cuisine"],
            preference_tags=list(rspec["tags"]), preparation_minutes=rspec["prep"],
            cooking_minutes=rspec["cook"], required_equipment=list(rspec["equipment"]) or None,
            leftover_reuse=None, storage_instructions=None, reheating_instructions=None,
            generated_by=None,
        )
        session.add(recipe)
        session.flush()
        diff.m("recipe").created += 1
        for canonical_name, quantity, optional, subgroup in rspec["ingredients"]:
            ingredient = ingredient_by_name[canonical_name]
            session.add(RecipeIngredient(
                recipe_id=recipe.id, ingredient_id=ingredient.id, canonical_name=canonical_name,
                display_name=ingredient.display_name, quantity=_d(quantity),
                unit=_recipe_unit(canonical_name, ingredient), optional=optional,
                substitution_group=subgroup,
            ))
            diff.m("recipe_ingredient").created += 1
        for step_number, instruction in enumerate(rspec["steps"], start=1):
            session.add(RecipeStep(
                recipe_id=recipe.id, step_number=step_number, instruction=instruction,
                duration_minutes=None,
            ))
            diff.m("recipe_step").created += 1

    if activate:
        _activate_demo(session, now, diff)

    session.flush()
    return diff


def _activate_demo(session: Session, now: datetime, diff: BootstrapDiff) -> None:
    """Enable the demo provider for the planner. Updates ONLY ProviderActivation code='demo'.

    catalog_readiness treats a provider as production-ready when production_enabled AND
    production_approved are both true; production_approved_by is nullable and is NOT required by
    that gate, so no human user is invented (the approval is a synthetic-bootstrap system action,
    recorded in ``notes``). The seven real providers are never touched.
    """
    row = session.execute(
        select(ProviderActivation).where(
            ProviderActivation.provider_code == DEMO_PROVIDER_CODE)
    ).scalar_one_or_none()
    if row is None:
        raise DemoBootstrapError("demo_activation_row_missing", DEMO_PROVIDER_CODE)
    if row.data_rights_status != "own_synthetic":
        raise DemoBootstrapError("demo_activation_rights_unexpected", row.data_rights_status)
    note = ("Synthetic demo catalog activation via bootstrap_demo_catalog "
            "(system bootstrap; not a human approval).")
    target = {
        "production_enabled": True, "production_approved": True, "production_eligibility": True,
        "costing_eligibility": "sufficient", "activation_state": "production_primary",
        "data_rights_status": "own_synthetic",
    }
    changed = any(getattr(row, k) != v for k, v in target.items()) \
        or row.production_approved_at is None or row.notes != note
    if not changed:
        diff.m("provider_activation").reused += 1
        diff.activated_demo = True
        return
    for k, v in target.items():
        setattr(row, k, v)
    if row.production_approved_at is None:
        row.production_approved_at = now
    row.production_approved_by = None  # never invent a human approver
    row.notes = note
    diff.m("provider_activation").updated += 1
    diff.activated_demo = True


def _internal_readiness_ok(session: Session) -> tuple[bool, dict[str, Any]]:
    report = catalog_readiness_report(session)
    ok = (report["status"] == "available" and not report["blockers"]
          and report["recipes_costable"] > 0 and report["approved_mappings"] > 0
          and report["productive_prices"] > 0 and report["production_ready_providers"] >= 1)
    return ok, report


def run(*, apply: bool, activate: bool) -> dict[str, Any]:
    """Execute the bootstrap in ONE transaction. dry-run rolls back; apply commits (only if the
    internal readiness projection is coherent). Returns a sanitized result dict."""
    session = SessionLocal()
    try:
        diff = bootstrap(session, activate=activate)
        ok, readiness = _internal_readiness_ok(session)
        result: dict[str, Any] = {
            "mode": "apply" if apply else "dry-run",
            "activate_demo": activate,
            "diff": diff.as_dict(),
            "readiness_projected": {k: readiness[k] for k in (
                "status", "recipes_active", "recipes_costable", "approved_mappings",
                "productive_prices", "production_ready_providers", "blockers")},
            "readiness_ok": ok,
        }
        if apply:
            if activate and not ok:
                session.rollback()
                result["committed"] = False
                result["error"] = "readiness_not_available_after_apply"
                return result
            session.commit()
            result["committed"] = True
        else:
            session.rollback()
            result["committed"] = False
        return result
    except DemoBootstrapError as exc:
        session.rollback()
        return {"mode": "apply" if apply else "dry-run", "activate_demo": activate,
                "committed": False, "error": exc.code, "detail": exc.detail}
    except Exception:
        session.rollback()
        return {"mode": "apply" if apply else "dry-run", "activate_demo": activate,
                "committed": False, "error": "unexpected_error"}
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="run + rollback (default)")
    mode.add_argument("--apply", action="store_true", help="run + commit in one transaction")
    act = p.add_mutually_exclusive_group()
    act.add_argument("--activate-demo", dest="activate", action="store_true")
    act.add_argument("--no-activate-demo", dest="activate", action="store_false")
    p.set_defaults(activate=False)
    a = p.parse_args(argv)
    result = run(apply=bool(a.apply), activate=a.activate)
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
