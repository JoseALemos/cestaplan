"""API tests for the read-only catalog router: retailers, stores, recipe detail + IDOR."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import (
    Household,
    Product,
    ProductPrice,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    Retailer,
    Store,
)

from .conftest import login, register


def _email() -> str:
    return f"catalog-{uuid.uuid4().hex[:12]}@example.com"


def _catalog_client(db_session: Session) -> TestClient:
    from cestaplan_api.routers import auth, catalog, households

    app = FastAPI()
    for module in (auth, households, catalog):
        app.include_router(module.router)

    def _override_get_db() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _a_public_recipe(db_session: Session) -> Recipe:
    recipe = db_session.execute(
        select(Recipe).where(Recipe.is_public.is_(True)).order_by(Recipe.id)
    ).scalars().first()
    assert recipe is not None
    return recipe


def _make_private_recipe(db_session: Session, household_id: int) -> Recipe:
    """A household-scoped private recipe (origin ai_generated) for IDOR checks."""
    recipe = Recipe(
        household_id=household_id,
        origin="ai_generated",
        is_public=False,
        is_synthetic=False,
        title="Receta privada",
        description="privada",
        servings=2,
        meal_types=["lunch"],
        cuisine="mediterranea",
        preference_tags=["rapida"],
        preparation_minutes=5,
        cooking_minutes=5,
        required_equipment=["stovetop"],
    )
    db_session.add(recipe)
    db_session.flush()
    ing = db_session.execute(select(RecipeIngredient).limit(1)).scalars().first()
    assert ing is not None
    db_session.add(
        RecipeIngredient(
            recipe_id=recipe.id,
            ingredient_id=ing.ingredient_id,
            canonical_name=ing.canonical_name,
            display_name=ing.display_name,
            quantity=ing.quantity,
            unit=ing.unit,
            optional=False,
            substitution_group=None,
        )
    )
    db_session.add(
        RecipeStep(recipe_id=recipe.id, step_number=1, instruction="Cocinar.")
    )
    db_session.flush()
    return recipe


def _seed_real_priced_retailer(db_session: Session) -> Retailer:
    """Una cadena NO sintética con una tienda que precia un producto real (visible en catálogo).

    ``list_retailers`` solo muestra cadenas con al menos un precio NO sintético (la demo
    MercaEjemplo, cuyos precios son todos sintéticos, queda oculta); esta es la contraparte
    visible que el endpoint debe listar.
    """
    retailer = Retailer(
        slug=f"real-{uuid.uuid4().hex[:8]}", name="Cadena Real", adapter_key="open_prices",
        country="ES", is_active=True, is_synthetic=False,
    )
    db_session.add(retailer)
    db_session.flush()
    store = Store(
        retailer_id=retailer.id, external_code=f"osm:{uuid.uuid4().hex[:8]}", name="Tienda Centro",
        province="Madrid", locality="Madrid", postal_code="28013", is_active=True,
        is_synthetic=False,
    )
    db_session.add(store)
    product = Product(
        retailer_id=retailer.id, external_id=uuid.uuid4().hex[:12], name="Leche entera 1 L",
        is_synthetic=False,
    )
    db_session.add_all([store, product])
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        ProductPrice(
            retailer_id=retailer.id, store_id=store.id, product_id=product.id,
            amount=Decimal("1.05"), currency="EUR", package_quantity=Decimal("1"),
            package_unit="l", source_type="open_dataset", source_name="Open Prices",
            observed_at=now, imported_at=now, confidence_score=Decimal("0.9"), is_synthetic=False,
        )
    )
    db_session.flush()
    return retailer


def test_list_retailers_and_stores(db_session: Session) -> None:
    # Cadenas solo-sintéticas (la demo MercaEjemplo) están OCULTAS al usuario; una cadena con
    # precios reales SÍ se lista (ver también test_catalog_hide_empty).
    real = _seed_real_priced_retailer(db_session)
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)

    retailers = client.get("/api/v1/retailers")
    assert retailers.status_code == 200, retailers.text
    body = retailers.json()
    ids = {r["id"] for r in body}
    assert str(real.public_id) in ids, "una cadena no sintética con precios reales debe listarse"

    shown = next(r for r in body if r["id"] == str(real.public_id))
    assert shown["is_synthetic"] is False
    assert uuid.UUID(shown["id"])
    assert shown["name"]
    # Costing metadata: whether the chain prices enough ingredients to cost plans.
    assert isinstance(shown["costing_supported"], bool)
    assert isinstance(shown["costable_ingredient_count"], int)
    assert shown["costable_ingredient_count"] >= 0

    stores = client.get(f"/api/v1/retailers/{shown['id']}/stores")
    assert stores.status_code == 200, stores.text
    stores_body = stores.json()
    assert stores_body
    store = stores_body[0]
    for key in (
        "id", "name", "province", "locality", "postal_code",
        "external_store_id", "catalog_updated_at", "price_coverage",
    ):
        assert key in store
    # price_coverage is a string (money/quantities never float across the boundary).
    assert store["price_coverage"] is None or isinstance(store["price_coverage"], str)


def test_retailers_requires_auth(db_session: Session) -> None:
    client = _catalog_client(db_session)
    assert client.get("/api/v1/retailers").status_code == 401


def test_stores_unknown_retailer_404(db_session: Session) -> None:
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)
    resp = client.get(f"/api/v1/retailers/{uuid.uuid4()}/stores")
    assert resp.status_code == 404


def test_recipe_detail_public_happy_path(db_session: Session) -> None:
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)

    recipe = _a_public_recipe(db_session)
    resp = client.get(f"/api/v1/recipes/{recipe.public_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == str(recipe.public_id)
    assert data["title"] == recipe.title
    assert data["ingredients"]
    # Quantity is serialized as a string.
    assert isinstance(data["ingredients"][0]["quantity"], str)
    assert data["steps"] and data["steps"][0]["position"] == 1
    assert isinstance(data["allergens"], list)


def test_recipe_detail_idor_other_household_private_404(db_session: Session) -> None:
    client = _catalog_client(db_session)

    # Owner household with a private recipe.
    owner_email = _email()
    register(client, owner_email)
    owner_token = login(client, owner_email)
    hh = client.post(
        "/api/v1/households",
        json={"name": "Casa"},
        headers={"X-CSRF-Token": owner_token},
    ).json()
    household = db_session.execute(
        select(Household).where(Household.public_id == uuid.UUID(hh["id"]))
    ).scalar_one()
    private = _make_private_recipe(db_session, household.id)

    # Owner (a member) can read it.
    ok = client.get(f"/api/v1/recipes/{private.public_id}")
    assert ok.status_code == 200, ok.text

    # A different user (not a member) gets 404, no existence disclosure.
    other_email = _email()
    register(client, other_email)
    login(client, other_email)
    resp = client.get(f"/api/v1/recipes/{private.public_id}")
    assert resp.status_code == 404


def test_recipe_detail_unknown_404(db_session: Session) -> None:
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)
    assert client.get(f"/api/v1/recipes/{uuid.uuid4()}").status_code == 404
