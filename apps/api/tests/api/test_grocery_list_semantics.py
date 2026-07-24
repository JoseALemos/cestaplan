"""Grocery-list serializer price/cost semantics (audit §5/§6/§10/§11) — DB-backed, no network.

Builds a small productive shopping list from ProductPrice (the productive path) and asserts the
serializer exposes a whole-package price (never a per-gram value), separated purchased/consumed/
leftover money, honest demo source kinds, and that staging/shadow data never enters the list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import (
    ExternalProduct,
    GroceryList,
    GroceryListItem,
    Household,
    MealPlan,
    PriceObservation,
    Product,
    ProductPrice,
    ProductVariant,
    User,
)
from cestaplan_api.services.plan_service import serialize_grocery_list
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_retailer,
    seed_test_store,
)

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _plan(db: Session) -> MealPlan:
    user = User(email=f"gl-{id(db)}@x.com", password_hash="x", display_name="GL")
    db.add(user)
    db.flush()
    hh = Household(name="Hogar test", owner_user_id=user.id)
    db.add(hh)
    db.flush()
    plan = MealPlan(household_id=hh.id, start_date=date(2026, 7, 21), end_date=date(2026, 7, 27))
    db.add(plan)
    db.flush()
    return plan


def _demo_price(db: Session, retailer_id: int, store_id: int, product: Product, amount: str) -> int:
    price = ProductPrice(
        retailer_id=retailer_id,
        store_id=store_id,
        product_id=product.id,
        amount=Decimal(amount),
        currency="EUR",
        package_quantity=Decimal("1"),
        package_unit="unit",
        source_type="demo",
        source_name="MercaEjemplo demo",
        observed_at=_NOW,
        imported_at=_NOW,
        confidence_score=Decimal("1.0"),
    )
    db.add(price)
    db.flush()
    return price.id


def _item(db: Session, gl: GroceryList, ing, prod, price_id, **over) -> GroceryListItem:
    base: dict = {
        "grocery_list_id": gl.id,
        "product_id": prod.id,
        "ingredient_id": ing.id,
        "needed_quantity": Decimal("109.5"),
        "pantry_quantity": Decimal("0"),
        "pending_quantity": Decimal("109.5"),
        "package_quantity": Decimal("500"),
        "package_unit": "ml",
        "packages_selected": 1,
        "purchased_quantity": Decimal("500"),
        "used_quantity": Decimal("109.5"),
        "leftover_quantity": Decimal("390.5"),
        "unit_price": Decimal("0.006380"),  # the mislabelled per-ml reference (ignored now)
        "price_product_price_id": price_id,
        "total_cost": Decimal("3.19"),
        "price_status": "known",
        "is_checked": False,
    }
    base.update(over)
    item = GroceryListItem(**base)  # type: ignore[arg-type]
    db.add(item)
    db.flush()
    return item


def _setup_list(db: Session) -> MealPlan:
    plan = _plan(db)
    retailer = seed_test_retailer(db, "mercaejemplo-test", name="MercaEjemplo")
    store = seed_test_store(db, retailer)
    gl = GroceryList(meal_plan_id=plan.id, currency="EUR", coverage_status="partial")
    db.add(gl)
    db.flush()

    aceite_ing = ensure_test_ingredient(db, "aceite_oliva", category_code="aceites_condimentos")
    aceite = Product(name="Aceite de oliva virgen extra MarcaDemo 500 ml", is_synthetic=True)
    db.add(aceite)
    db.flush()
    p1 = _demo_price(db, retailer.id, store.id, aceite, "3.19")
    _item(db, gl, aceite_ing, aceite, p1)

    # Garbanzos: 600 g needed, 400 g jars, 2 jars -> 800 g purchased, 1.62 € outlay.
    garb_ing = ensure_test_ingredient(db, "garbanzos", category_code="legumbres")
    garb = Product(name="Garbanzos cocidos 400 g", is_synthetic=True)
    db.add(garb)
    db.flush()
    p2 = _demo_price(db, retailer.id, store.id, garb, "0.81")
    _item(
        db,
        gl,
        garb_ing,
        garb,
        p2,
        needed_quantity=Decimal("600"),
        pending_quantity=Decimal("600"),
        package_quantity=Decimal("400"),
        package_unit="g",
        packages_selected=2,
        purchased_quantity=Decimal("800"),
        used_quantity=Decimal("600"),
        leftover_quantity=Decimal("200"),
        total_cost=Decimal("1.62"),
    )
    return plan


def test_package_price_is_whole_package_never_per_gram(db_session: Session) -> None:
    plan = _setup_list(db_session)
    out = serialize_grocery_list(db_session, plan)
    aceite = next(
        it
        for cat in out["categories"]
        for it in cat["items"]
        if "Aceite" in (it["product_name"] or "")
    )
    assert aceite["package_price"] == "3.19"  # the real €/envase, NOT 0,01
    assert aceite["normalized_unit_price"] == "6.38"
    assert aceite["normalized_unit"] == "l"
    assert aceite["required_quantity"] == "109.5000" and aceite["required_unit"] == "ml"
    assert aceite["price_source_kind"] == "demo"


def test_multi_package_line_distinguishes_unit_and_total(db_session: Session) -> None:
    plan = _setup_list(db_session)
    out = serialize_grocery_list(db_session, plan)
    garb = next(
        it
        for cat in out["categories"]
        for it in cat["items"]
        if "Garbanzos" in (it["product_name"] or "")
    )
    assert garb["packages_required"] == 2
    assert garb["package_price"] == "0.81"  # one jar
    assert garb["purchased_cost"] == "1.62"  # two jars
    assert garb["purchased_quantity"] == "800.0000"
    assert garb["leftover_quantity"] == "200.0000"


def test_costs_are_separated_and_demo_sources_counted(db_session: Session) -> None:
    plan = _setup_list(db_session)
    out = serialize_grocery_list(db_session, plan)
    # Purchased outlay = full packages (3.19 + 1.62); consumed < purchased; leftover = diff.
    assert out["purchase_outlay"] == "4.81"
    assert Decimal(out["consumed_cost"]) < Decimal(out["purchase_outlay"])
    assert Decimal(out["leftover_value"]) == Decimal(out["purchase_outlay"]) - Decimal(
        out["consumed_cost"]
    )
    assert out["total_items"] == 2
    assert out["source_counts"] == {
        "demo": 2,
        "confirmed_external": 0,
        "estimated": 0,
        "unavailable": 0,
    }


def test_staging_and_shadow_never_enter_the_productive_list(db_session: Session) -> None:
    plan = _setup_list(db_session)
    # A staging observation on the same chain must be invisible to the productive grocery list,
    # which reads only persisted ProductPrice — never PriceObservation/staging/shadow.
    retailer = seed_test_retailer(db_session, "mercaejemplo-test")
    noise = Product(name="Ruido staging", is_synthetic=True)
    db_session.add(noise)
    db_session.flush()
    ext = ExternalProduct(retailer_id=retailer.id, external_id="STG-NOISE")
    db_session.add(ext)
    db_session.flush()
    variant = ProductVariant(
        retailer_id=retailer.id,
        external_product_id=ext.id,
        product_id=noise.id,
        display_name="Ruido",
        sell_unit="package",
        net_content_quantity=Decimal("1000"),
        net_content_unit="ml",
    )
    db_session.add(variant)
    db_session.flush()
    db_session.add(
        PriceObservation(
            retailer_id=retailer.id,
            product_variant_id=variant.id,
            price_scope="national",
            price_type="regular",
            amount=Decimal("9.99"),
            currency="EUR",
            observed_at=_NOW,
            imported_at=_NOW,
            valid_from=_NOW,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    db_session.flush()

    out = serialize_grocery_list(db_session, plan)
    # Still exactly the two demo lines; the staging noise never appears, no external/shadow source.
    assert out["total_items"] == 2
    kinds = {it["price_source_kind"] for cat in out["categories"] for it in cat["items"]}
    assert kinds == {"demo"}
    names = {it["product_name"] for cat in out["categories"] for it in cat["items"]}
    assert "Ruido staging" not in names
