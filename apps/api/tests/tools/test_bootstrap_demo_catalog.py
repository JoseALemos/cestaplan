"""Additive, idempotent demo-catalog bootstrap — coexistence + safety tests.

Every test builds a base that mirrors the production situation: pre-existing NON-synthetic
recipes / ingredients (including canonical names that collide with the demo dataset), products
tied to real retailers, zero prices, zero mappings, and a demo ``ProviderActivation`` that exists
but is switched off. The tool must load the synthetic MercaEjemplo catalogue additively without
touching any of that, and a second run must be a no-op.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductPrice,
    ProviderActivation,
    Recipe,
    RecipeIngredient,
    Retailer,
)
from cestaplan_api.scripts import seed_demo
from cestaplan_api.seed import data as seed_data
from cestaplan_api.services.catalog_readiness import catalog_readiness_report
from cestaplan_api.tools import bootstrap_demo_catalog as boot

# The seven real provider codes that must always stay OFF.
_REAL_PROVIDERS = ("parsebot-alcampo", "parsebot-dia", "parsebot-carrefour", "parsebot-lidl",
                   "parsebot-aldi", "parsebot-deza", "apify-mercadona")
_OVERLAP_NAMES = [spec["name"] for spec in seed_data.INGREDIENTS[:17]]  # collide with the demo set


def _seed_production_like_base(db: Session) -> dict[str, object]:
    """Recreate the prod situation inside the test transaction and return baseline counts.

    The suite's session-scoped ``_ensure_demo_seed`` fixture (tests/api) may have COMMITTED the
    synthetic demo catalogue. We start from a clean synthetic universe *within this rolled-back
    transaction* (wipe synthetic rows + clear activations) so the tool is exercised on a fresh
    slate; the teardown rollback restores whatever the session seed committed. This wipe runs in
    test SETUP only — never under the SQL listener that asserts the tool emits no DELETE.
    """
    seed_demo._wipe_synthetic(db)
    db.execute(delete(ProviderActivation))
    db.flush()
    # A real chain with a couple of real products that carry NO price (must never be touched).
    real = Retailer(slug="real-chain-x", name="Real Chain X", adapter_key="parsebot",
                    is_synthetic=False)
    db.add(real)
    db.flush()
    real_products = []
    for i in range(2):
        p = Product(retailer_id=real.id, external_id=f"REAL-{i}", name=f"Real product {i}",
                    is_synthetic=False)
        db.add(p)
        real_products.append(p)
    # 17 NON-synthetic ingredients whose canonical_name collides with the demo dataset.
    for name in _OVERLAP_NAMES:
        db.add(Ingredient(canonical_name=name, display_name=f"Pre {name}", category_code="x",
                          default_unit="g", is_synthetic=False))
    # A NON-synthetic ingredient used only by a base recipe (keeps that recipe UNcostable).
    only = Ingredient(canonical_name="__base_only_ingredient__", display_name="Base only",
                      category_code="x", default_unit="g", is_synthetic=False)
    db.add(only)
    db.flush()
    # A NON-synthetic public recipe referencing the base-only ingredient (stays uncostable).
    base_recipe = Recipe(household_id=None, origin="imported", is_public=True, is_synthetic=False,
                         title="Receta base no sintetica", description="d", servings=2,
                         meal_types=["lunch"], cuisine="x", preference_tags=[],
                         preparation_minutes=5, cooking_minutes=5)
    db.add(base_recipe)
    db.flush()
    db.add(RecipeIngredient(recipe_id=base_recipe.id, ingredient_id=only.id,
                            canonical_name=only.canonical_name, display_name="Base only",
                            quantity=1, unit="g", optional=False, substitution_group=None))
    # Provider activations: seven real (off) + demo (rights own_synthetic, off).
    for code in _REAL_PROVIDERS:
        db.add(ProviderActivation(provider_code=code, data_rights_status="commercial_use_allowed",
                                  authorization_status="verified",
                                  license_basis="private_commercial_agreement"))
    db.add(ProviderActivation(provider_code="demo", data_rights_status="own_synthetic",
                              authorization_status="verified", license_basis="own_synthetic"))
    db.flush()
    return {
        "real_retailer_id": real.id,
        "real_product_ids": [p.id for p in real_products],
        "base_recipe_id": base_recipe.id,
    }


def _counts(db: Session) -> dict[str, int]:
    def c(model, *w):
        q = select(func.count()).select_from(model)
        for x in w:
            q = q.where(x)
        return int(db.scalar(q) or 0)
    return {
        "ingredient": c(Ingredient),
        "product": c(Product),
        "product_price": c(ProductPrice),
        "mapping": c(IngredientProductMapping),
        "recipe": c(Recipe),
        "retailer": c(Retailer),
    }


# --------------------------------------------------------------------------- #
# 1-9: additive reuse / creation / scoping / costability
# --------------------------------------------------------------------------- #
def test_reuses_overlapping_ingredients_and_creates_only_missing(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    diff = boot.bootstrap(db_session, activate=False)
    # 17 collide -> reused; the remaining demo ingredients -> created.
    assert diff.models["ingredient"].reused == 17
    assert set(diff.reused_ingredient_names) == set(_OVERLAP_NAMES)
    assert diff.models["ingredient"].created == len(seed_data.INGREDIENTS) - 17
    assert diff.models["ingredient"].updated == 0


def test_does_not_modify_reused_ingredient_fields(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    before = {
        i.canonical_name: (i.display_name, i.is_synthetic, i.category_code)
        for i in db_session.execute(
            select(Ingredient).where(Ingredient.canonical_name.in_(_OVERLAP_NAMES))).scalars()
    }
    boot.bootstrap(db_session, activate=False)
    after = {
        i.canonical_name: (i.display_name, i.is_synthetic, i.category_code)
        for i in db_session.execute(
            select(Ingredient).where(Ingredient.canonical_name.in_(_OVERLAP_NAMES))).scalars()
    }
    assert before == after  # names, is_synthetic and metadata untouched
    # every reused ingredient is still NON-synthetic.
    assert all(not v[1] for v in after.values())


def test_does_not_touch_non_synthetic_recipes(db_session: Session) -> None:
    ids = _seed_production_like_base(db_session)
    before = db_session.get(Recipe, ids["base_recipe_id"])
    assert before is not None
    snapshot = (before.title, before.is_synthetic, before.is_public)
    boot.bootstrap(db_session, activate=False)
    after = db_session.get(Recipe, ids["base_recipe_id"])
    assert after is not None
    assert (after.title, after.is_synthetic, after.is_public) == snapshot
    assert after.is_synthetic is False


def test_does_not_touch_real_products_or_price_them(db_session: Session) -> None:
    ids = _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)
    # real products still carry ZERO prices; no price references a real retailer.
    real_priced = db_session.scalar(
        select(func.count()).select_from(ProductPrice).where(
            ProductPrice.product_id.in_(list(ids["real_product_ids"]))))  # type: ignore[arg-type]
    assert real_priced == 0
    real_retailer_prices = db_session.scalar(
        select(func.count()).select_from(ProductPrice).where(
            ProductPrice.retailer_id == ids["real_retailer_id"]))
    assert real_retailer_prices == 0


def test_all_new_products_prices_mappings_belong_to_mercaejemplo(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)
    demo = db_session.execute(
        select(Retailer).where(Retailer.slug == seed_data.RETAILER_SLUG)).scalar_one()
    assert demo.is_synthetic is True
    # every synthetic product belongs to MercaEjemplo.
    syn_products = db_session.execute(
        select(Product).where(Product.is_synthetic.is_(True))).scalars().all()
    assert syn_products and all(p.retailer_id == demo.id for p in syn_products)
    # every price is demo, synthetic, positive, EUR, MercaEjemplo, never confirmed_external.
    prices = db_session.execute(select(ProductPrice)).scalars().all()
    assert prices
    for pr in prices:
        assert pr.retailer_id == demo.id
        assert pr.is_synthetic is True
        assert pr.source_type == "demo"
        assert pr.currency == "EUR"
        assert pr.amount > 0
    # every active mapping is scoped to MercaEjemplo and human_verified.
    maps = db_session.execute(select(IngredientProductMapping)).scalars().all()
    assert maps
    for mp in maps:
        assert mp.retailer_id == demo.id
        assert mp.is_active is True
        assert mp.verification_status == "human_verified"


def test_demo_recipes_are_costable_and_readiness_available(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)
    report = catalog_readiness_report(db_session)
    assert report["status"] == "available"
    assert report["blockers"] == []
    # all 92 demo recipes are costable (the single base recipe is not).
    assert report["recipes_costable"] == len(seed_data.RECIPES)
    assert report["production_ready_providers"] == 1
    assert report["approved_mappings"] > 0
    assert report["productive_prices"] > 0


# --------------------------------------------------------------------------- #
# 10-13: idempotency
# --------------------------------------------------------------------------- #
def test_first_run_creates_second_run_creates_nothing(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    first = boot.bootstrap(db_session, activate=True)
    assert first.models["product"].created > 0
    assert first.models["product_price"].created > 0
    assert first.models["recipe"].created == len(seed_data.RECIPES)
    counts_after_first = _counts(db_session)
    second = boot.bootstrap(db_session, activate=True)
    for name, md in second.models.items():
        assert md.created == 0, f"{name} created {md.created} on second run"
    assert second.deletes == 0
    assert _counts(db_session) == counts_after_first  # byte-identical counts


def test_deletes_always_zero(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    diff = boot.bootstrap(db_session, activate=True)
    assert diff.deletes == 0


def test_no_delete_truncate_drop_sql_is_emitted(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    seen: list[str] = []

    def _rec(conn, cursor, statement, params, context, executemany):
        seen.append(statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "")

    event.listen(db_session.bind, "before_cursor_execute", _rec)
    try:
        boot.bootstrap(db_session, activate=True)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", _rec)
    assert "DELETE" not in seen
    assert "TRUNCATE" not in seen
    assert "DROP" not in seen
    # the ONLY UPDATE permitted is the demo provider activation row.
    updates = [s for s in seen if s == "UPDATE"]
    assert len(updates) <= 1


# --------------------------------------------------------------------------- #
# 14: fail-closed on a non-synthetic natural-key collision + rollback
# --------------------------------------------------------------------------- #
def test_fail_closed_when_demo_slug_owned_by_non_synthetic(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    # A NON-synthetic retailer squats the demo slug.
    db_session.add(Retailer(slug=seed_data.RETAILER_SLUG, name="Impostor", adapter_key="x",
                            is_synthetic=False))
    db_session.flush()
    with pytest.raises(boot.DemoBootstrapError) as ei:
        boot.bootstrap(db_session, activate=False)
    assert ei.value.code == "demo_natural_key_owned_by_non_synthetic_row"


def test_ambiguous_ingredient_identity_would_fail_closed(db_session: Session) -> None:
    # Two rows for one canonical_name (simulated) -> ambiguous -> fail closed. The DB has a unique
    # index, so we assert the guard directly against a stubbed result.
    class _S:
        def scalars(self):
            class _R:
                def all(self):
                    return [object(), object()]
            return _R()

    class _DB:
        def execute(self, *_a, **_k):
            return _S()

    with pytest.raises(boot.DemoBootstrapError) as ei:
        boot._get_or_fail_ingredient(_DB(), "dup")  # type: ignore[arg-type]
    assert ei.value.code == "ingredient_identity_ambiguous"


# --------------------------------------------------------------------------- #
# 16-18: no network; activation scoped to demo; real providers stay OFF
# --------------------------------------------------------------------------- #
def test_makes_no_network_calls(db_session: Session, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("network access is forbidden in the demo bootstrap")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)  # must complete without any socket use


def test_activation_only_affects_demo_and_real_providers_stay_off(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)
    rows = {pa.provider_code: pa for pa in db_session.execute(
        select(ProviderActivation)).scalars()}
    demo = rows["demo"]
    assert demo.production_enabled is True and demo.production_approved is True
    assert demo.data_rights_status == "own_synthetic"
    assert demo.production_approved_by is None  # no human approver invented
    assert demo.costing_eligibility == "sufficient"
    for code in _REAL_PROVIDERS:
        assert rows[code].production_enabled is False
        assert rows[code].production_approved is False


def test_no_activate_leaves_provider_off_but_still_loads_catalog(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    diff = boot.bootstrap(db_session, activate=False)
    assert diff.activated_demo is False
    demo = db_session.execute(select(ProviderActivation).where(
        ProviderActivation.provider_code == "demo")).scalar_one()
    assert demo.production_enabled is False
    # catalogue still loaded (products/prices/mappings present) though not planner-available.
    assert diff.models["product"].created > 0
    report = catalog_readiness_report(db_session)
    assert "no_production_approved_provider" in report["blockers"]


# --------------------------------------------------------------------------- #
# 20: prices are labelled demo (source of the "Precio demo" UI label)
# --------------------------------------------------------------------------- #
def test_prices_carry_the_demo_label_markers(db_session: Session) -> None:
    _seed_production_like_base(db_session)
    boot.bootstrap(db_session, activate=True)
    prices = db_session.execute(select(ProductPrice)).scalars().all()
    assert prices
    assert all(p.source_type == "demo" and p.is_synthetic is True for p in prices)
    assert all(p.source_type != "confirmed_external" for p in prices)


# --------------------------------------------------------------------------- #
# demo recipe ingredient units must be the DATASET base unit (costable), even when the
# ingredient is REUSED from a pre-existing row whose default_unit differs.
# --------------------------------------------------------------------------- #
def _one_demo_recipe_ingredient(db: Session, canonical: str):
    return db.execute(
        select(RecipeIngredient)
        .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
        .where(Recipe.is_synthetic.is_(True), RecipeIngredient.canonical_name == canonical)
    ).scalars().first()


def test_reused_ingredient_recipe_uses_dataset_unit_not_default_unit(db_session: Session) -> None:
    seed_demo._wipe_synthetic(db_session)
    db_session.execute(delete(ProviderActivation))
    db_session.flush()
    # A pre-existing NON-synthetic "cebolla" in a human-friendly unit (dataset base unit is "g").
    db_session.add(Ingredient(canonical_name="cebolla", display_name="Cebolla",
                              category_code="verduras", default_unit="unidad", is_synthetic=False))
    db_session.add(ProviderActivation(
        provider_code="demo", data_rights_status="own_synthetic",
        authorization_status="verified", license_basis="own_synthetic"))
    db_session.flush()
    boot.bootstrap(db_session, activate=True)
    ri = _one_demo_recipe_ingredient(db_session, "cebolla")
    assert ri is not None
    assert ri.unit == "g"  # dataset base unit, NOT the reused row's "unidad"
    # the reused ingredient row itself is unchanged (still non-synthetic, still "unidad").
    ing = db_session.execute(
        select(Ingredient).where(Ingredient.canonical_name == "cebolla")).scalar_one()
    assert ing.is_synthetic is False and ing.default_unit == "unidad"


def test_self_heals_existing_demo_recipe_ingredient_units(db_session: Session) -> None:
    seed_demo._wipe_synthetic(db_session)
    db_session.execute(delete(ProviderActivation))
    db_session.flush()
    db_session.add(ProviderActivation(
        provider_code="demo", data_rights_status="own_synthetic",
        authorization_status="verified", license_basis="own_synthetic"))
    db_session.flush()
    boot.bootstrap(db_session, activate=True)          # first load (correct units)
    ri = _one_demo_recipe_ingredient(db_session, "cebolla")
    assert ri is not None and ri.unit == "g"
    ri.unit = "unidad"                                  # corrupt an existing demo unit
    db_session.flush()
    diff = boot.bootstrap(db_session, activate=True)    # re-run -> self-heal
    assert diff.models["recipe_ingredient"].updated >= 1
    assert diff.models["recipe_ingredient"].created == 0
    db_session.refresh(ri)
    assert ri.unit == "g"
