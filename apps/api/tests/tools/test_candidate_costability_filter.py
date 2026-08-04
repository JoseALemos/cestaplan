"""build_seed_candidates restricts to recipes the selected retailer can fully cost.

Regression guard for the beta: with unpriced public recipes present, the optimizer must not be
offered recipes whose mandatory ingredients the chosen chain cannot price (they would produce a
plan full of "unavailable" costs). Optional ingredients may be unpriced; an empty allow_list keeps
the old behaviour (no filter).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Ingredient,
    Product,
    ProductPrice,
    ProviderActivation,
    Recipe,
    RecipeIngredient,
    Retailer,
    Store,
)
from cestaplan_api.scripts import seed_demo
from cestaplan_api.services.candidate_providers import build_seed_candidates


def _clean(db: Session) -> None:
    seed_demo._wipe_synthetic(db)
    db.execute(delete(ProviderActivation))
    db.flush()


def _ing(db: Session, name: str) -> Ingredient:
    i = Ingredient(canonical_name=name, display_name=name.title(), category_code="x",
                   default_unit="g", is_synthetic=True)
    db.add(i)
    db.flush()
    return i


def _priced_product(db: Session, retailer: Retailer, store: Store, ing: Ingredient,
                    ext: str) -> None:
    p = Product(retailer_id=retailer.id, external_id=ext, name=f"{ing.display_name} pack",
                package_quantity=Decimal(1000), package_unit="g", is_synthetic=True)
    db.add(p)
    db.flush()
    now = datetime.now(UTC)
    db.add(ProductPrice(retailer_id=retailer.id, store_id=store.id, product_id=p.id,
                        amount=Decimal("2.00"), currency="EUR", package_quantity=Decimal(1000),
                        package_unit="g", unit_price=Decimal("0.002"), availability="in_stock",
                        source_type="demo", source_name="Filter demo", observed_at=now,
                        imported_at=now, confidence_score=Decimal("1.0"), is_synthetic=True))
    db.flush()


def _recipe(db: Session, title: str, mandatory: list[Ingredient],
            optional: list[Ingredient] | None = None) -> Recipe:
    r = Recipe(household_id=None, origin="seed", is_public=True, is_synthetic=True, title=title,
               description="d", servings=2, meal_types=["lunch"], cuisine="x",
               preference_tags=[], preparation_minutes=5, cooking_minutes=5)
    db.add(r)
    db.flush()
    for ing in mandatory:
        db.add(RecipeIngredient(recipe_id=r.id, ingredient_id=ing.id,
                                canonical_name=ing.canonical_name, display_name=ing.display_name,
                                quantity=Decimal(100), unit="g", optional=False,
                                substitution_group=None))
    for ing in optional or []:
        db.add(RecipeIngredient(recipe_id=r.id, ingredient_id=ing.id,
                                canonical_name=ing.canonical_name, display_name=ing.display_name,
                                quantity=Decimal(10), unit="g", optional=True,
                                substitution_group=None))
    db.flush()
    return r


def _setup(db: Session):
    _clean(db)
    retailer = Retailer(slug="filter-chain", name="Filter Chain", adapter_key="demo",
                        is_synthetic=True)
    db.add(retailer)
    db.flush()
    store = Store(retailer_id=retailer.id, external_code="FC-1", name="S", is_synthetic=True)
    db.add(store)
    db.flush()
    priced_a = _ing(db, "filt_priced_a")
    priced_b = _ing(db, "filt_priced_b")
    unpriced = _ing(db, "filt_unpriced")
    _priced_product(db, retailer, store, priced_a, "FA")
    _priced_product(db, retailer, store, priced_b, "FB")
    # unpriced ingredient has NO product/price.
    costable = _recipe(db, "Costable lunch", [priced_a, priced_b])
    uncostable = _recipe(db, "Uncostable lunch", [priced_a, unpriced])
    optional_ok = _recipe(db, "Optional-unpriced lunch", [priced_a], optional=[unpriced])
    allow_list = ["filt_priced_a", "filt_priced_b"]
    return allow_list, costable, uncostable, optional_ok


def test_filter_excludes_recipes_with_unpriced_mandatory_ingredient(db_session: Session) -> None:
    allow_list, costable, uncostable, optional_ok = _setup(db_session)
    titles = {c.title for c in build_seed_candidates(db_session, {"lunch"}, allow_list)}
    assert costable.title in titles
    assert optional_ok.title in titles       # unpriced ingredient is OPTIONAL -> still offered
    assert uncostable.title not in titles     # unpriced MANDATORY -> excluded


def test_empty_allow_list_disables_the_filter(db_session: Session) -> None:
    _allow, costable, uncostable, optional_ok = _setup(db_session)
    for al in ([], None):
        titles = {c.title for c in build_seed_candidates(db_session, {"lunch"}, al)}
        assert {costable.title, uncostable.title, optional_ok.title} <= titles
