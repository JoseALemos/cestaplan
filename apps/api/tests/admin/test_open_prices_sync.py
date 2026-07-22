"""Open Prices sync service + admin endpoint tests — HTTPX mocked, NO network.

Covers: sync_store creating real Product + ProductBarcode + ProductPrice from mocked
prices; idempotency (run twice -> no duplicate observations, append-only preserved); prices
tagged ``open_dataset`` / ``is_synthetic=False`` with ODbL provenance; the no-barcode skip;
and the admin endpoint authz (non-admin 403) + disabled-source (409) gates.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OpenPricesAdapter
from cestaplan_api.models import (
    Product,
    ProductBarcode,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services import open_prices_sync

from .conftest import csrf, login, promote_to_admin, register

_OSM_ID = 677280352
_OSM_TYPE = "WAY"

_PAGE = {
    "items": [
        {
            "id": 101,
            "product_code": "8410000000001",
            "product_name": "Leche entera",
            "price": 0.95,
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": None,
        },
        {
            "id": 102,  # no barcode -> skipped
            "product_code": None,
            "product_name": "ESPARRAGO",
            "price": 3.89,
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": "UNIT",
        },
        {
            "id": 103,
            "product_code": "8410000000002",
            "product_name": "Manzanas",
            "price": 1.80,
            "currency": "EUR",
            "date": "2026-04-11",
            "price_per": "KILOGRAM",
        },
    ],
    "page": 1,
    "pages": 1,
    "size": 100,
    "total": 3,
}


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}@example.com"


def _op_adapter(payload: dict | None = None, *, status_code: int = 200) -> OpenPricesAdapter:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload if payload is not None else _PAGE)

    return OpenPricesAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _make_store(db: Session) -> Store:
    retailer = Retailer(
        slug=f"lidl-{uuid.uuid4().hex[:8]}",
        name="Lidl",
        adapter_key="open_prices",
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(
        retailer_id=retailer.id,
        external_code=open_prices_sync.store_external_code(_OSM_TYPE, _OSM_ID),
        name="Lidl",
        locality="Sant Joan d'Alacant",
        postal_code="03550",
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


# --------------------------------------------------------------------------- #
# Service: creates real product/barcode/price; provenance; no-barcode skip
# --------------------------------------------------------------------------- #
def test_sync_creates_products_barcodes_prices(db_session: Session) -> None:
    store = _make_store(db_session)
    summary = open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())

    assert summary.fetched == 3
    assert summary.inserted == 2  # two barcoded rows
    assert summary.skipped_no_barcode == 1
    assert summary.products_created == 2

    products = db_session.execute(
        select(Product).where(Product.retailer_id == store.retailer_id)
    ).scalars().all()
    assert {p.external_id for p in products} == {"8410000000001", "8410000000002"}
    assert all(p.is_synthetic is False for p in products)

    barcodes = db_session.execute(
        select(ProductBarcode.barcode).join(
            Product, Product.id == ProductBarcode.product_id
        ).where(Product.retailer_id == store.retailer_id)
    ).scalars().all()
    assert set(barcodes) == {"8410000000001", "8410000000002"}

    prices = db_session.execute(
        select(ProductPrice).where(ProductPrice.store_id == store.id)
    ).scalars().all()
    assert len(prices) == 2
    for pr in prices:
        assert pr.source_type == "open_dataset"
        assert pr.is_synthetic is False
        assert pr.source_name == "Open Food Facts - Open Prices"
        assert (pr.source_url or "").startswith(
            "https://prices.openfoodfacts.org/prices/"
        )
        assert pr.currency == "EUR"
        assert pr.import_id is not None


def test_sync_is_idempotent(db_session: Session) -> None:
    store = _make_store(db_session)

    def price_count() -> int:
        return db_session.execute(
            select(func.count(ProductPrice.id)).where(ProductPrice.store_id == store.id)
        ).scalar_one()

    open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())
    first = price_count()
    second_summary = open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())
    second = price_count()

    assert first == 2
    assert second == 2  # no duplicate observations on re-sync (append-only preserved)
    assert second_summary.inserted == 0
    assert second_summary.skipped_existing == 2


def test_sync_appends_new_observation_for_new_date(db_session: Session) -> None:
    store = _make_store(db_session)
    open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())

    newer = {
        "items": [
            {
                "id": 999,
                "product_code": "8410000000001",
                "product_name": "Leche entera",
                "price": 1.05,
                "currency": "EUR",
                "date": "2026-05-01",  # a later observation of the same product
            }
        ],
        "page": 1,
        "pages": 1,
    }
    summary = open_prices_sync.sync_store(db_session, store, adapter=_op_adapter(newer))
    assert summary.inserted == 1  # appended, not overwritten
    total = db_session.execute(
        select(func.count(ProductPrice.id))
        .join(Product, Product.id == ProductPrice.product_id)
        .where(Product.external_id == "8410000000001", ProductPrice.store_id == store.id)
    ).scalar_one()
    assert total == 2  # both dated observations coexist


def test_sync_provenance_is_odbl(db_session: Session) -> None:
    store = _make_store(db_session)
    summary = open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())
    assert summary.license_code == "ODbL"
    assert "Open Prices" in summary.attribution


def test_sync_unit_price_derivation(db_session: Session) -> None:
    store = _make_store(db_session)
    open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())
    # KILOGRAM basis -> unit_price derivable (= amount, €/kg); packaged (None basis) -> null.
    apples = db_session.execute(
        select(ProductPrice)
        .join(Product, Product.id == ProductPrice.product_id)
        .where(Product.external_id == "8410000000002")
    ).scalar_one()
    assert apples.package_unit == "kg"
    assert apples.unit_price is not None
    milk = db_session.execute(
        select(ProductPrice)
        .join(Product, Product.id == ProductPrice.product_id)
        .where(Product.external_id == "8410000000001")
    ).scalar_one()
    assert milk.package_unit == "unit"
    assert milk.unit_price is None  # no basis reported -> not fabricated


def test_sync_bad_external_code_is_graceful(db_session: Session) -> None:
    retailer = Retailer(
        slug=f"x-{uuid.uuid4().hex[:8]}", name="X", adapter_key="open_prices",
        country="ES", is_active=True, is_synthetic=False,
    )
    db_session.add(retailer)
    db_session.flush()
    store = Store(retailer_id=retailer.id, external_code="not-osm", is_active=True)
    db_session.add(store)
    db_session.flush()
    summary = open_prices_sync.sync_store(db_session, store, adapter=_op_adapter())
    assert summary.inserted == 0
    assert summary.errors  # a logged error, no crash


# --------------------------------------------------------------------------- #
# Admin endpoint: authz + disabled gate
# --------------------------------------------------------------------------- #
def test_admin_sync_requires_admin(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post(
        "/api/v1/admin/sources/open-prices/sync", json={}, headers=csrf(token)
    )
    assert resp.status_code == 403


def test_admin_sync_requires_csrf(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post("/api/v1/admin/sources/open-prices/sync", json={})
    assert resp.status_code == 403


def test_admin_sync_disabled_returns_409(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    ds = open_prices_sync.ensure_open_prices_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()
    resp = client.post(
        "/api/v1/admin/sources/open-prices/sync", json={}, headers=csrf(token)
    )
    assert resp.status_code == 409


def test_admin_sync_single_store(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        open_prices_sync, "OpenPricesAdapter", lambda: _op_adapter()
    )
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    store = _make_store(db_session)
    resp = client.post(
        "/api/v1/admin/sources/open-prices/sync",
        json={"store_id": str(store.public_id)},
        headers=csrf(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stores_synced"] == 1
    assert body["inserted"] == 2
    assert "ODbL" in body["license_code"]
    assert "Open Prices" in body["attribution"]
