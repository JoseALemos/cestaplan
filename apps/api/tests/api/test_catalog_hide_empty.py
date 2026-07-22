"""Catalog hide-empty tests: retailers/stores show only when they have priced products.

A retailer or store with no ``ProductPrice`` is hidden (e.g. seeded-but-unsynced Open
Prices stores, or a chain like Deza with no data); one with at least one priced product is
returned. The synthetic demo retailer (which has prices) stays visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import Product, ProductPrice, Retailer, Store

from .conftest import login, register


def _email() -> str:
    return f"hide-{uuid.uuid4().hex[:12]}@example.com"


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


def _make_retailer(db: Session, slug_prefix: str) -> Retailer:
    retailer = Retailer(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
        name=f"{slug_prefix.title()} {uuid.uuid4().hex[:4]}",
        adapter_key="open_prices",
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    return retailer


def _make_store(db: Session, retailer: Retailer) -> Store:
    store = Store(
        retailer_id=retailer.id,
        external_code=f"osm:WAY/{uuid.uuid4().int % 10**9}",
        name="Tienda",
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


def _add_price(db: Session, retailer: Retailer, store: Store) -> None:
    product = Product(
        retailer_id=retailer.id,
        external_id=f"8410{uuid.uuid4().int % 10**9}",
        name="Producto",
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        ProductPrice(
            retailer_id=retailer.id,
            store_id=store.id,
            product_id=product.id,
            amount=Decimal("1.25"),
            currency="EUR",
            package_quantity=Decimal("1"),
            package_unit="unit",
            source_type="open_dataset",
            source_name="Open Food Facts - Open Prices",
            observed_at=now,
            imported_at=now,
            confidence_score=Decimal("0.5"),
            is_synthetic=False,
        )
    )
    db.flush()


def test_retailer_without_prices_is_hidden(db_session: Session) -> None:
    empty = _make_retailer(db_session, "empty")
    _make_store(db_session, empty)  # a store, but no prices

    priced = _make_retailer(db_session, "priced")
    priced_store = _make_store(db_session, priced)
    _add_price(db_session, priced, priced_store)

    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)

    resp = client.get("/api/v1/retailers")
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()}
    assert str(priced.public_id) in ids
    assert str(empty.public_id) not in ids


def test_empty_store_hidden_priced_store_shown(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "chain")
    empty_store = _make_store(db_session, retailer)
    priced_store = _make_store(db_session, retailer)
    _add_price(db_session, retailer, priced_store)

    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)

    resp = client.get(f"/api/v1/retailers/{retailer.public_id}/stores")
    assert resp.status_code == 200, resp.text
    stores = resp.json()
    ids = {s["id"] for s in stores}
    assert str(priced_store.public_id) in ids
    assert str(empty_store.public_id) not in ids
    shown = next(s for s in stores if s["id"] == str(priced_store.public_id))
    assert shown["priced_product_count"] == 1


def test_synthetic_demo_retailer_stays_visible(db_session: Session) -> None:
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    login(client, email)
    resp = client.get("/api/v1/retailers")
    assert resp.status_code == 200
    # The demo seed (MercaEjemplo, synthetic + priced) is present in the shared DB.
    assert any(r["is_synthetic"] for r in resp.json())
