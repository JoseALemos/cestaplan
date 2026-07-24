"""Ingredient-matching service + admin endpoint tests.

Covers: the matcher maps clear cases (name + OFF category) and rejects doubtful ones
(snack/nougat guards, tuna-in-oil not mapped to oil, wrong category); the unit-compatibility
guard; ``map_real_products`` idempotency; a store with mapped priced products yields
real-priced catalog lines in ``planning_context``; chain-level coverage; and the admin
endpoint authz (non-admin 403) + happy path.

Runs against the live DB inside a rolled-back transaction (the 75 canonical ingredients are
already seeded there); every product/price it needs is created inside the test transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services import ingredient_matching
from cestaplan_api.services.planning_context import _build_catalog

from ..fixtures.provider_scenarios import ensure_test_ingredient
from .conftest import csrf, login, promote_to_admin, register


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _index(db: Session) -> dict[str, Ingredient]:
    return ingredient_matching._load_ingredient_index(db)


def _product(name: str, *, category_code: str | None = None) -> Product:
    """A transient (un-persisted) real product for pure-matching assertions."""
    return Product(name=name, category_code=category_code, is_synthetic=False, retailer_id=None)


def _retailer(db: Session, slug: str) -> Retailer:
    r = Retailer(slug=slug, name=slug.title(), adapter_key="test", country="ES")
    db.add(r)
    db.flush()
    return r


def _store(db: Session, retailer: Retailer) -> Store:
    s = Store(retailer_id=retailer.id, name="Tienda test")
    db.add(s)
    db.flush()
    return s


def _real_product(db: Session, retailer: Retailer, name: str, **kw: object) -> Product:
    p = Product(retailer_id=retailer.id, name=name, is_synthetic=False, **kw)
    db.add(p)
    db.flush()
    return p


def _price(
    db: Session,
    store: Store,
    product: Product,
    amount: str,
    *,
    package_unit: str = "unit",
) -> ProductPrice:
    now = datetime.now(UTC)
    price = ProductPrice(
        retailer_id=store.retailer_id,
        store_id=store.id,
        product_id=product.id,
        amount=Decimal(amount),
        currency="EUR",
        package_quantity=Decimal("1"),
        package_unit=package_unit,
        source_type="open_dataset",
        source_name="Open Prices",
        observed_at=now,
        imported_at=now,
        confidence_score=Decimal("0.5"),
        is_synthetic=False,
    )
    db.add(price)
    db.flush()
    return price


# --------------------------------------------------------------------------- #
# match_product — clear cases
# --------------------------------------------------------------------------- #
def test_matches_clear_name_cases(db_session: Session) -> None:
    # Hermetic: create exactly the canonical ingredients these curated name rules map onto.
    for name, unit in [
        ("aceite_oliva", "ml"),
        ("pavo_pechuga", "g"),
        ("tofu", "g"),
        ("leche_entera", "ml"),
        ("pimenton", "g"),
    ]:
        ensure_test_ingredient(db_session, name, default_unit=unit)
    idx = _index(db_session)
    cases = {
        "Aceite de Oliva Virgen Extra 1L": "aceite_oliva",
        "Pechuga de pavo fresca": "pavo_pechuga",
        "Tofu natural ecológico": "tofu",
        "Leche entera fresca 1L": "leche_entera",
        "Pimentón dulce La Chinata": "pimenton",
    }
    for name, canonical in cases.items():
        result = ingredient_matching.match_product(db_session, _product(name), ingredient_index=idx)
        assert result is not None, name
        assert result[0].canonical_name == canonical, name
        assert Decimal("0.70") <= result[1] <= Decimal("1")


def test_matches_off_category(db_session: Session) -> None:
    ensure_test_ingredient(db_session, "arroz_basmati", default_unit="g")
    idx = _index(db_session)
    result = ingredient_matching.match_product(
        db_session, _product("Producto 123", category_code="basmati-rices"), ingredient_index=idx
    )
    assert result is not None
    assert result[0].canonical_name == "arroz_basmati"
    assert result[1] >= Decimal("0.9")


# --------------------------------------------------------------------------- #
# match_product — rejections (conservative)
# --------------------------------------------------------------------------- #
def test_rejects_snack_and_processed(db_session: Session) -> None:
    idx = _index(db_session)
    for name in (
        "Turrón blando de cacahuete",
        "Chocolate negro 70%",
        "Galletas María",
        "Tomate frito Solís",
        "Pipas de girasol con sal",
    ):
        assert (
            ingredient_matching.match_product(db_session, _product(name), ingredient_index=idx)
            is None
        ), name


def test_tuna_in_oil_maps_to_tuna_not_oil(db_session: Session) -> None:
    # Both exist so the matcher demonstrably prefers tuna over oil (not by oil's absence).
    ensure_test_ingredient(db_session, "atun_lata", default_unit="g")
    ensure_test_ingredient(db_session, "aceite_oliva", default_unit="ml")
    idx = _index(db_session)
    result = ingredient_matching.match_product(
        db_session, _product("Atún claro en aceite de oliva"), ingredient_index=idx
    )
    assert result is not None
    assert result[0].canonical_name == "atun_lata"


def test_rejects_wrong_category(db_session: Session) -> None:
    idx = _index(db_session)
    result = ingredient_matching.match_product(
        db_session,
        _product("Producto X", category_code="carbonated-drinks"),
        ingredient_index=idx,
    )
    assert result is None


def test_unit_incompatible_rejected(db_session: Session) -> None:
    """A liquid ingredient (aceite_oliva, ml) is not mapped to a mass-priced product."""
    retailer = _retailer(db_session, "unittest")
    store = _store(db_session, retailer)
    product = _real_product(db_session, retailer, "Aceite de oliva virgen extra")
    _price(db_session, store, product, "5.00", package_unit="kg")  # mass basis → conflict
    idx = _index(db_session)
    assert ingredient_matching.match_product(db_session, product, ingredient_index=idx) is None


# --------------------------------------------------------------------------- #
# map_real_products — idempotency + coverage + catalog
# --------------------------------------------------------------------------- #
def test_map_real_products_idempotent_and_priced_catalog(db_session: Session) -> None:
    ensure_test_ingredient(db_session, "aceite_oliva", default_unit="ml")
    retailer = _retailer(db_session, "chaintest")
    store = _store(db_session, retailer)
    oil = _real_product(db_session, retailer, "Aceite de oliva suave 1L")
    junk = _real_product(db_session, retailer, "Chocolate negro 70%")
    _price(db_session, store, oil, "4.09")
    _price(db_session, store, junk, "1.99")

    first = ingredient_matching.map_real_products(db_session, store_id=store.id)
    assert first.mapped == 1  # only the oil; the chocolate is (correctly) unmatched
    assert first.unmatched == 1

    mapping = db_session.execute(
        select(IngredientProductMapping).where(IngredientProductMapping.product_id == oil.id)
    ).scalar_one()
    ingredient = db_session.get(Ingredient, mapping.ingredient_id)
    assert ingredient is not None and ingredient.canonical_name == "aceite_oliva"
    assert mapping.confidence_score is not None and mapping.confidence_score > Decimal("0.7")
    assert mapping.retailer_id == retailer.id

    # Re-running maps nothing new (idempotent).
    second = ingredient_matching.map_real_products(db_session, store_id=store.id)
    assert second.mapped == 0

    # planning_context now yields a real-priced catalog line for the mapped ingredient.
    # Catalog pricing is chain-scoped (per retailer, aggregating all its stores), so the
    # catalog is built for the chain the priced store belongs to.
    catalog = _build_catalog(db_session, retailer.id)
    oil_line = next((c for c in catalog if c.canonical_name == "aceite_oliva"), None)
    assert oil_line is not None
    assert oil_line.packages and oil_line.packages[0].has_price
    assert oil_line.packages[0].amount == Decimal("4.09")
    assert oil_line.packages[0].source_type == "open_dataset"

    # Chain-level coverage counts the priced ingredient.
    coverage = ingredient_matching.chain_ingredient_coverage(db_session, retailer.id)
    assert int(coverage["priced_ingredients"]) >= 1  # type: ignore[arg-type]
    assert "aceite_oliva" in coverage["ingredients"]  # type: ignore[operator]


# --------------------------------------------------------------------------- #
# Admin endpoint
# --------------------------------------------------------------------------- #
def test_map_ingredients_endpoint_requires_admin(client: TestClient) -> None:
    register(client, "user@example.com")
    token = login(client, "user@example.com")
    resp = client.post("/api/v1/admin/sources/map-ingredients", json={}, headers=csrf(token))
    assert resp.status_code == 403


def test_map_ingredients_endpoint_happy_path(client: TestClient, db_session: Session) -> None:
    register(client, "admin@example.com")
    promote_to_admin(db_session, "admin@example.com")
    token = login(client, "admin@example.com")

    resp = client.post("/api/v1/admin/sources/map-ingredients", json={}, headers=csrf(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "mapped" in body
    assert "per_chain" in body
    assert "chain_coverage" in body
