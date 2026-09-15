"""Phase 2: compare_plan_with_travel adds travel cost on top of the pure Phase-1 comparison.

DB-backed, hermetic, NO NETWORK: geocoding/Overpass are never called here — the household
already carries coordinates and a warm ``HouseholdChainStore`` cache for every visible chain,
exactly the shape ``travel_by_retailer`` reads without refreshing (see
``services/geo/household_geo.py``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import (
    Household,
    HouseholdChainStore,
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
from cestaplan_api.services.geo.travel import travel_cost_eur
from cestaplan_api.services.plan_comparison import compare_plan_with_travel
from tests.fixtures.provider_scenarios import ensure_test_ingredient, seed_test_recipe

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _chain(db: Session, slug: str, name: str) -> tuple[Retailer, Store]:
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


def _household(db: Session, *, with_address: bool) -> Household:
    user = User(
        email=f"trv-{id(db)}-{with_address}@x.com", password_hash="x", display_name="Trv"
    )
    db.add(user)
    db.flush()
    household = Household(name="Hogar viaje", owner_user_id=user.id, currency="EUR")
    if with_address:
        household.address_text = "Calle Falsa 123"
        household.postal_code = "14006"
        household.city = "Córdoba"
        household.latitude = Decimal("37.8882")
        household.longitude = Decimal("-4.7794")
        household.geocode_status = "ok"
        household.geocoded_at = _NOW
    db.add(household)
    db.flush()
    return household


def _plan_with_meal(
    db: Session, household: Household, recipe_id: int, *, servings: int = 2
) -> MealPlan:
    plan = MealPlan(
        household_id=household.id,
        start_date=date(2026, 9, 15),
        end_date=date(2026, 9, 21),
        currency="EUR",
        status="ready",
    )
    db.add(plan)
    db.flush()
    db.add(
        PlannedMeal(
            meal_plan_id=plan.id,
            recipe_id=recipe_id,
            scheduled_date=date(2026, 9, 15),
            meal_type="lunch",
            servings=servings,
            status="planned",
        )
    )
    db.flush()
    return plan


def _cache_store(
    db: Session, household: Household, retailer: Retailer, *, distance_km: str
) -> None:
    db.add(
        HouseholdChainStore(
            household_id=household.id,
            retailer_id=retailer.id,
            store_name=f"{retailer.name} cercana",
            latitude=Decimal("37.9000"),
            longitude=Decimal("-4.8000"),
            distance_km=Decimal(distance_km),
            found=True,
            computed_at=_NOW,
            source="overpass",
        )
    )
    db.flush()


def _chain_by_name(result: dict, name: str) -> dict:
    return next(c for c in result["chains"] if c["retailer_name"] == name)


def _build_basket_and_chains(
    db: Session, *, suffix: str
) -> tuple[MealPlan, Household, Retailer, Retailer]:
    arroz = ensure_test_ingredient(db, f"arroz_{suffix}", category_code="cereales")
    tomate = ensure_test_ingredient(db, f"tomate_{suffix}", category_code="frutas")
    aceite = ensure_test_ingredient(db, f"aceite_{suffix}", category_code="aceites_condimentos")
    recipe = seed_test_recipe(
        db,
        f"Paella {suffix}",
        [
            (arroz, "300", "g", False),
            (tomate, "200", "g", False),
            (aceite, "100", "ml", False),
        ],
        servings=2,
    )
    household = _household(db, with_address=suffix == "addr")
    plan = _plan_with_meal(db, household, recipe.id, servings=2)

    alcampo, a_store = _chain(db, f"alcampo-{suffix}", "Alcampo")
    dia, d_store = _chain(db, f"dia-{suffix}", "Dia")
    # Alcampo: 1.50 + 1.00 + 5.00 = 7.50 (full coverage).
    _sell(db, alcampo, a_store, arroz, pkg_qty="1000", pkg_unit="g", amount="1.50")
    _sell(db, alcampo, a_store, tomate, pkg_qty="500", pkg_unit="g", amount="1.00")
    _sell(db, alcampo, a_store, aceite, pkg_qty="1000", pkg_unit="ml", amount="5.00")
    # Dia: 0.90 + 1.20 + 3.00 = 5.10 (full coverage, cheaper).
    _sell(db, dia, d_store, arroz, pkg_qty="500", pkg_unit="g", amount="0.90")
    _sell(db, dia, d_store, tomate, pkg_qty="400", pkg_unit="g", amount="1.20")
    _sell(db, dia, d_store, aceite, pkg_qty="500", pkg_unit="ml", amount="3.00")

    return plan, household, alcampo, dia


def test_travel_added_when_household_has_address(db_session: Session) -> None:
    plan, household, alcampo, dia = _build_basket_and_chains(db_session, suffix="addr")
    # Alcampo 5 km, Dia 15 km away -> distinct travel costs (defaults: 1.3 detour, 0.26 €/km).
    _cache_store(db_session, household, alcampo, distance_km="5")
    _cache_store(db_session, household, dia, distance_km="15")

    settings = Settings()
    out = compare_plan_with_travel(db_session, plan, settings, household)

    assert out["travel"]["enabled"] is True
    assert out["travel"]["has_address"] is True
    assert out["travel"]["rate_eur_per_km"] == "0.26"
    assert out["travel"]["detour_factor"] == "1.3"

    alcampo_travel_cost = travel_cost_eur(Decimal("5"), settings)
    dia_travel_cost = travel_cost_eur(Decimal("15"), settings)
    assert alcampo_travel_cost == Decimal("3.38")
    assert dia_travel_cost == Decimal("10.14")

    a = _chain_by_name(out, "Alcampo")
    d = _chain_by_name(out, "Dia")
    assert Decimal(a["travel"]["distance_km"]) == Decimal("5")
    assert Decimal(a["travel"]["travel_cost"]) == alcampo_travel_cost
    assert a["travel"]["found"] is True
    assert Decimal(a["total_with_travel"]) == Decimal(a["known_cost"]) + alcampo_travel_cost
    assert Decimal(d["total_with_travel"]) == Decimal(d["known_cost"]) + dia_travel_cost

    # best_single_with_travel: both chains cover fully; Alcampo (7.50+3.38=10.88) beats
    # Dia (5.10+10.14=15.24) once travel is added, even though Dia was cheaper on price alone.
    best = out["best_single_with_travel"]
    assert best is not None
    assert best["retailer_name"] == "Alcampo"
    assert Decimal(best["total_with_travel"]) == Decimal("10.88")
    assert Decimal(best["basket_cost"]) == Decimal("7.50")
    assert Decimal(best["travel_cost"]) == alcampo_travel_cost

    # Split visits both chains (arroz+aceite -> Dia, tomate -> Alcampo): travel_total sums
    # each DISTINCT chain's trip once.
    split = out["split"]
    assert split["distinct_chain_count"] == 2
    expected_travel_total = alcampo_travel_cost + dia_travel_cost
    assert Decimal(split["travel_total"]) == expected_travel_total
    assert Decimal(split["total_with_travel"]) == Decimal(split["total"]) + expected_travel_total
    assert Decimal(split["savings_vs_best_single_with_travel"]) == (
        Decimal(best["total_with_travel"]) - Decimal(split["total_with_travel"])
    )


def test_no_travel_without_household_address(db_session: Session) -> None:
    plan, household, _alcampo, _dia = _build_basket_and_chains(db_session, suffix="noaddr")
    assert household.latitude is None and household.longitude is None

    settings = Settings()
    out = compare_plan_with_travel(db_session, plan, settings, household)

    assert out["travel"]["has_address"] is False
    assert out["travel"]["enabled"] is True
    for chain in out["chains"]:
        assert "travel" not in chain
        assert "total_with_travel" not in chain
    assert "best_single_with_travel" not in out
    assert "travel_total" not in out["split"]
