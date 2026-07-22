"""API tests for the "Precios reales" viewer endpoint: GET .../stores/{id}/prices.

Covers: only real (``open_dataset``, non-synthetic) prices are returned, money/quantities
are strings, the latest observation wins per product, search filters by product name, ODbL
attribution is present, IDOR (store must belong to the given retailer) and the direct-call
empty-store case (hidden from the store picker, but a graceful 200 with no items here).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import Product, ProductBarcode, ProductPrice, Retailer, Store

from .conftest import login, register


def _email() -> str:
    return f"prices-{uuid.uuid4().hex[:12]}@example.com"


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
        name="Tienda Real",
        locality="Madrid",
        postal_code="28001",
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


def _make_product(db: Session, retailer: Retailer, name: str, brand: str | None = None) -> Product:
    product = Product(
        retailer_id=retailer.id,
        external_id=f"8410{uuid.uuid4().int % 10**9}",
        name=name,
        brand=brand,
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    return product


def _add_price(
    db: Session,
    retailer: Retailer,
    store: Store,
    product: Product,
    *,
    amount: Decimal = Decimal("1.25"),
    observed_at: datetime | None = None,
    source_type: str = "open_dataset",
    is_synthetic: bool = False,
    source_url: str | None = "https://prices.openfoodfacts.org/prices/1",
) -> ProductPrice:
    observed_at = observed_at or datetime.now(UTC)
    price = ProductPrice(
        retailer_id=retailer.id,
        store_id=store.id,
        product_id=product.id,
        amount=amount,
        currency="EUR",
        package_quantity=Decimal("1"),
        package_unit="unit",
        unit_price=amount,
        source_type=source_type,
        source_name="Open Food Facts - Open Prices",
        source_url=source_url,
        observed_at=observed_at,
        imported_at=observed_at,
        confidence_score=Decimal("0.5"),
        is_synthetic=is_synthetic,
    )
    db.add(price)
    db.flush()
    return price


def _login(db_session: Session) -> tuple[TestClient, str]:
    client = _catalog_client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    return client, token


def test_store_prices_returns_real_only_with_string_money(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "aldi")
    store = _make_store(db_session, retailer)
    real_product = _make_product(db_session, retailer, "Leche entera 1L", brand="Marca Blanca")
    db_session.add(
        ProductBarcode(product_id=real_product.id, barcode="8410000000001", is_primary=True)
    )
    _add_price(db_session, retailer, store, real_product)

    # A synthetic/non-open_dataset row at the same store must never leak into the viewer.
    synthetic_product = _make_product(db_session, retailer, "Producto sintético")
    _add_price(
        db_session,
        retailer,
        store,
        synthetic_product,
        source_type="demo",
        is_synthetic=True,
        source_url=None,
    )

    client, _ = _login(db_session)
    resp = client.get(
        f"/api/v1/retailers/{retailer.public_id}/stores/{store.public_id}/prices"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["count"] == 1
    assert body["store"]["name"] == "Tienda Real"
    assert body["store"]["locality"] == "Madrid"
    assert body["attribution"]
    assert body["license_code"] == "ODbL"

    item = body["items"][0]
    assert item["product_name"] == "Leche entera 1L"
    assert item["brand"] == "Marca Blanca"
    assert item["barcode"] == "8410000000001"
    assert isinstance(item["amount"], str)
    assert isinstance(item["unit_price"], str)
    assert item["currency"] == "EUR"
    assert item["source_type"] == "open_dataset"
    assert item["is_synthetic"] is False
    assert item["source_url"]


def test_store_prices_latest_observation_wins(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "lidl")
    store = _make_store(db_session, retailer)
    product = _make_product(db_session, retailer, "Pan de molde")
    older = datetime.now(UTC) - timedelta(days=10)
    newer = datetime.now(UTC)
    _add_price(db_session, retailer, store, product, amount=Decimal("1.10"), observed_at=older)
    _add_price(db_session, retailer, store, product, amount=Decimal("1.35"), observed_at=newer)

    client, _ = _login(db_session)
    resp = client.get(
        f"/api/v1/retailers/{retailer.public_id}/stores/{store.public_id}/prices"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["amount"] == "1.3500"


def test_store_prices_search_filters_by_product_name(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "carrefour")
    store = _make_store(db_session, retailer)
    tomate = _make_product(db_session, retailer, "Tomate frito")
    yogur = _make_product(db_session, retailer, "Yogur natural")
    _add_price(db_session, retailer, store, tomate)
    _add_price(db_session, retailer, store, yogur)

    client, _ = _login(db_session)
    resp = client.get(
        f"/api/v1/retailers/{retailer.public_id}/stores/{store.public_id}/prices",
        params={"search": "tomate"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["product_name"] == "Tomate frito"


def test_store_prices_idor_store_of_other_retailer_404(db_session: Session) -> None:
    retailer_a = _make_retailer(db_session, "dia")
    retailer_b = _make_retailer(db_session, "alcampo")
    store_b = _make_store(db_session, retailer_b)
    product_b = _make_product(db_session, retailer_b, "Producto B")
    _add_price(db_session, retailer_b, store_b, product_b)

    client, _ = _login(db_session)
    # store_b belongs to retailer_b, not retailer_a: must not be reachable via retailer_a.
    resp = client.get(
        f"/api/v1/retailers/{retailer_a.public_id}/stores/{store_b.public_id}/prices"
    )
    assert resp.status_code == 404


def test_store_prices_unknown_retailer_or_store_404(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "mercadona")
    store = _make_store(db_session, retailer)

    client, _ = _login(db_session)
    assert (
        client.get(
            f"/api/v1/retailers/{uuid.uuid4()}/stores/{store.public_id}/prices"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/retailers/{retailer.public_id}/stores/{uuid.uuid4()}/prices"
        ).status_code
        == 404
    )


def test_store_prices_requires_auth(db_session: Session) -> None:
    retailer = _make_retailer(db_session, "eroski")
    store = _make_store(db_session, retailer)
    client = _catalog_client(db_session)
    resp = client.get(
        f"/api/v1/retailers/{retailer.public_id}/stores/{store.public_id}/prices"
    )
    assert resp.status_code == 401


def test_store_prices_empty_store_direct_call_is_graceful(db_session: Session) -> None:
    """A store with zero real prices is hidden from the picker, but a direct call to its
    prices endpoint (e.g. a stale link) must degrade to an empty list, not an error."""
    retailer = _make_retailer(db_session, "consum")
    store = _make_store(db_session, retailer)  # no ProductPrice rows at all

    client, _ = _login(db_session)
    resp = client.get(
        f"/api/v1/retailers/{retailer.public_id}/stores/{store.public_id}/prices"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 0
    assert body["items"] == []
    assert body["attribution"]
