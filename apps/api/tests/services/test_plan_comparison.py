"""Multi-chain price comparison service (Phase 1) — DB-backed, hermetic, no network.

Builds real (non-synthetic) chains with DIFFERING prices, a plan with mandatory + optional
ingredients, and asserts: per-chain whole-package totals, best_single selection, the cross-chain
split picking the cheapest chain per item, savings vs best_single, that optionals are excluded,
and that an ingredient no chain prices is reported uncovered (never guessed).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Household,
    Ingredient,
    IngredientProductMapping,
    MealPlan,
    PlannedMeal,
    Product,
    ProductPrice,
    Retailer,
    Store,
    User,
)
from cestaplan_api.services.plan_comparison import compare_plan_across_chains
from tests.fixtures.provider_scenarios import ensure_test_ingredient, seed_test_recipe

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _chain(db: Session, slug: str, name: str) -> tuple[Retailer, Store]:
    """A real (non-synthetic) chain + one store — visible to the comparison."""
    retailer = Retailer(slug=slug, name=name, adapter_key="test", is_synthetic=False)
    db.add(retailer)
    db.flush()
    store = Store(retailer_id=retailer.id, name=f"{name} centro", is_synthetic=False)
    db.add(store)
    db.flush()
    return retailer, store


def _sell(
    db: Session,
    retailer: Retailer,
    store: Store,
    ingredient: Ingredient,
    *,
    pkg_qty: str,
    pkg_unit: str,
    amount: str,
) -> None:
    """Map ``ingredient`` to a product priced at ``retailer`` (one package format)."""
    product = Product(
        name=f"{ingredient.canonical_name} {retailer.slug}",
        package_quantity=Decimal(pkg_qty),
        package_unit=pkg_unit,
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    db.add(
        ProductPrice(
            retailer_id=retailer.id,
            store_id=store.id,
            product_id=product.id,
            amount=Decimal(amount),
            currency="EUR",
            package_quantity=Decimal(pkg_qty),
            package_unit=pkg_unit,
            source_type="manual_entry",
            source_name="prueba",
            observed_at=_NOW,
            imported_at=_NOW,
            confidence_score=Decimal("1.0"),
            is_synthetic=False,
        )
    )
    db.add(
        IngredientProductMapping(
            ingredient_id=ingredient.id,
            product_id=product.id,
            is_active=True,
            preference_rank=1,
        )
    )
    db.flush()


def _plan_with_meal(db: Session, recipe_id: int, *, servings: int = 2) -> MealPlan:
    user = User(
        email=f"cmp-{id(db)}-{recipe_id}@x.com", password_hash="x", display_name="Cmp"
    )
    db.add(user)
    db.flush()
    household = Household(name="Hogar comparación", owner_user_id=user.id, currency="EUR")
    db.add(household)
    db.flush()
    plan = MealPlan(
        household_id=household.id,
        start_date=date(2026, 7, 21),
        end_date=date(2026, 7, 27),
        currency="EUR",
        status="ready",
    )
    db.add(plan)
    db.flush()
    db.add(
        PlannedMeal(
            meal_plan_id=plan.id,
            recipe_id=recipe_id,
            scheduled_date=date(2026, 7, 21),
            meal_type="lunch",
            servings=servings,
            status="planned",
        )
    )
    db.flush()
    return plan


def _chain_by_name(result: dict, name: str) -> dict:
    return next(c for c in result["chains"] if c["retailer_name"] == name)


def test_full_coverage_comparison_and_split(db_session: Session) -> None:
    # Basket (mandatory, 2 servings, recipe base 2 -> scale 1): arroz 300 g, tomate 200 g,
    # aceite 100 ml. "sal" is OPTIONAL and must be excluded from the costed basket.
    arroz = ensure_test_ingredient(db_session, "arroz_cmp", category_code="cereales")
    tomate = ensure_test_ingredient(db_session, "tomate_cmp", category_code="frutas")
    aceite = ensure_test_ingredient(db_session, "aceite_cmp", category_code="aceites_condimentos")
    sal = ensure_test_ingredient(db_session, "sal_cmp", category_code="aceites_condimentos")
    recipe = seed_test_recipe(
        db_session,
        "Paella comparación",
        [
            (arroz, "300", "g", False),
            (tomate, "200", "g", False),
            (aceite, "100", "ml", False),
            (sal, "10", "g", True),  # optional: excluded from the basket
        ],
        servings=2,
    )
    plan = _plan_with_meal(db_session, recipe.id, servings=2)

    alcampo, a_store = _chain(db_session, "alcampo-cmp", "Alcampo")
    dia, d_store = _chain(db_session, "dia-cmp", "Dia")

    # Alcampo: arroz 1000 g @1.50 (1 pack), tomate 500 g @1.00, aceite 1000 ml @5.00 -> 7.50.
    _sell(db_session, alcampo, a_store, arroz, pkg_qty="1000", pkg_unit="g", amount="1.50")
    _sell(db_session, alcampo, a_store, tomate, pkg_qty="500", pkg_unit="g", amount="1.00")
    _sell(db_session, alcampo, a_store, aceite, pkg_qty="1000", pkg_unit="ml", amount="5.00")
    # Dia: arroz 500 g @0.90 (cheaper), tomate 400 g @1.20 (dearer), aceite 500 ml @3.00 -> 5.10.
    _sell(db_session, dia, d_store, arroz, pkg_qty="500", pkg_unit="g", amount="0.90")
    _sell(db_session, dia, d_store, tomate, pkg_qty="400", pkg_unit="g", amount="1.20")
    _sell(db_session, dia, d_store, aceite, pkg_qty="500", pkg_unit="ml", amount="3.00")

    out = compare_plan_across_chains(db_session, plan)

    # Optional "sal" is never in the basket or any chain's costs.
    assert out["basket"]["ingredient_count"] == 3
    basket_names = {i["canonical_name"] for i in out["basket"]["ingredients"]}
    assert basket_names == {"arroz_cmp", "tomate_cmp", "aceite_cmp"}

    # Per-chain whole-package totals + full coverage.
    a = _chain_by_name(out, "Alcampo")
    d = _chain_by_name(out, "Dia")
    assert Decimal(a["known_cost"]) == Decimal("7.50")
    assert Decimal(d["known_cost"]) == Decimal("5.10")
    assert a["full_coverage"] is True and d["full_coverage"] is True
    assert a["coverage"]["with_price"] == 3 and a["coverage"]["without_price"] == 0
    assert Decimal(a["coverage"]["ratio"]) == Decimal("1")
    # Per-ingredient cost map (whole-package outlay per required quantity).
    assert Decimal(a["ingredient_costs"]["arroz_cmp"]) == Decimal("1.50")
    assert Decimal(d["ingredient_costs"]["arroz_cmp"]) == Decimal("0.90")
    assert Decimal(d["ingredient_costs"]["aceite_cmp"]) == Decimal("3.00")

    # best_single: both cover fully -> cheapest total wins (Dia 5.10 < Alcampo 7.50).
    assert out["best_single"]["retailer_name"] == "Dia"
    assert Decimal(out["best_single"]["total"]) == Decimal("5.10")
    assert out["best_single"]["full_coverage"] is True

    # Split: arroz -> Dia (0.90), tomate -> Alcampo (1.00), aceite -> Dia (3.00) = 4.90.
    split = out["split"]
    assert Decimal(split["total"]) == Decimal("4.90")
    assert split["distinct_chain_count"] == 2
    assert not split["uncovered_ingredients"]
    # savings = best_single (5.10) - split (4.90) = 0.20.
    assert Decimal(split["savings_vs_best_single"]) == Decimal("0.20")

    by_chain = {c["retailer_name"]: c for c in split["by_chain"]}
    dia_items = {i["canonical_name"]: Decimal(i["cost"]) for i in by_chain["Dia"]["items"]}
    alcampo_items = {i["canonical_name"]: Decimal(i["cost"]) for i in by_chain["Alcampo"]["items"]}
    assert dia_items == {"arroz_cmp": Decimal("0.90"), "aceite_cmp": Decimal("3.00")}
    assert alcampo_items == {"tomate_cmp": Decimal("1.00")}
    assert Decimal(by_chain["Dia"]["subtotal"]) == Decimal("3.90")


def test_uncovered_ingredient_is_reported_not_guessed(db_session: Session) -> None:
    # Basket: arroz + tomate (priced by both chains) and azafran (priced by NO chain).
    arroz = ensure_test_ingredient(db_session, "arroz_unc", category_code="cereales")
    tomate = ensure_test_ingredient(db_session, "tomate_unc", category_code="frutas")
    azafran = ensure_test_ingredient(db_session, "azafran_unc", category_code="aceites_condimentos")
    recipe = seed_test_recipe(
        db_session,
        "Arroz con azafrán",
        [
            (arroz, "300", "g", False),
            (tomate, "200", "g", False),
            (azafran, "1", "g", False),  # mandatory but unmapped/unpriced everywhere
        ],
        servings=2,
    )
    plan = _plan_with_meal(db_session, recipe.id, servings=2)

    alcampo, a_store = _chain(db_session, "alcampo-unc", "Alcampo")
    dia, d_store = _chain(db_session, "dia-unc", "Dia")
    _sell(db_session, alcampo, a_store, arroz, pkg_qty="1000", pkg_unit="g", amount="1.50")
    _sell(db_session, alcampo, a_store, tomate, pkg_qty="500", pkg_unit="g", amount="1.00")
    _sell(db_session, dia, d_store, arroz, pkg_qty="500", pkg_unit="g", amount="0.90")
    _sell(db_session, dia, d_store, tomate, pkg_qty="400", pkg_unit="g", amount="1.20")

    out = compare_plan_across_chains(db_session, plan)

    # azafran is a mandatory basket ingredient but no chain prices it.
    assert out["basket"]["ingredient_count"] == 3
    for chain in out["chains"]:
        assert "azafran_unc" not in chain["ingredient_costs"]  # never invented
        assert chain["coverage"]["with_price"] == 2
        assert chain["coverage"]["without_price"] == 1
        assert Decimal(chain["coverage"]["ratio"]) < Decimal("1")

    split = out["split"]
    uncovered = {i["canonical_name"] for i in split["uncovered_ingredients"]}
    assert uncovered == {"azafran_unc"}
    # The split still buys the covered items at their cheapest chain (arroz+tomate -> 0.90+1.00).
    assert Decimal(split["total"]) == Decimal("1.90")
    split_items = {
        i["canonical_name"]
        for c in split["by_chain"]
        for i in c["items"]
    }
    assert split_items == {"arroz_unc", "tomate_unc"}

    # 2/3 coverage is below the near-full threshold, so no single chain is offered as a
    # defensible "shop it all here" answer (unequal baskets are never compared as if equal).
    assert out["best_single"] is None
    assert split["savings_vs_best_single"] is None
