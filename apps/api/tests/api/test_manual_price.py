"""API tests for manual price entry (spec §17, FASE E).

``POST /api/v1/admin/prices/manual`` is the operator interface to record a hand-observed price
as a first-class ``manual`` :class:`PriceObservation`. These tests assert it is admin-only and
CSRF-guarded, creates an append-only manual observation (never fabricated), returns money as
strings, projects the current price for the engine, and rejects a non-positive amount. Everything
runs inside the shared transactional ``db_session`` and is rolled back on teardown.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.models import (
    PriceObservation,
    Product,
    ProductPrice,
    Retailer,
    Store,
    User,
)
from cestaplan_api.routers import auth as auth_router
from cestaplan_api.routers import ingestion_admin

from .conftest import csrf, login, register


def _client(db_session: Session) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(ingestion_admin.router)

    def _override_get_db() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _email() -> str:
    return f"manualprice-{uuid.uuid4().hex[:12]}@example.com"


def _make_admin(client: TestClient, db_session: Session) -> str:
    email = _email()
    register(client, email)
    user = db_session.execute(select(User).where(User.email == email)).scalar_one()
    user.is_admin = True
    db_session.flush()
    return login(client, email)


def _make_user(client: TestClient) -> str:
    email = _email()
    register(client, email)
    return login(client, email)


def _make_retailer(db: Session) -> Retailer:
    retailer = Retailer(
        slug=f"man-{uuid.uuid4().hex[:8]}",
        name="Manual Retailer",
        adapter_key="demo",
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
        external_code=f"code-{uuid.uuid4().hex[:8]}",
        name="Tienda",
        locality="Madrid",
        postal_code="28001",
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


# --------------------------------------------------------------------------- #
# Auth / CSRF
# --------------------------------------------------------------------------- #
def test_manual_price_requires_admin(db_session: Session) -> None:
    client = _client(db_session)
    _make_user(client)  # non-admin session
    retailer = _make_retailer(db_session)
    resp = client.post(
        "/api/v1/admin/prices/manual",
        json={"retailer_code": retailer.slug, "amount": "1.50", "barcode": "8410000000001"},
    )
    assert resp.status_code == 403, resp.text


def test_manual_price_requires_csrf(db_session: Session) -> None:
    client = _client(db_session)
    _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    # No CSRF header -> 403 even for an admin.
    resp = client.post(
        "/api/v1/admin/prices/manual",
        json={"retailer_code": retailer.slug, "amount": "1.50", "barcode": "8410000000001"},
    )
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_manual_price_creates_manual_observation_and_projects(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    store = _make_store(db_session, retailer)

    resp = client.post(
        "/api/v1/admin/prices/manual",
        headers=csrf(token),
        json={
            "retailer_code": retailer.slug,
            "store_id": str(store.public_id),
            "barcode": "8410000000001",
            "amount": "1.49",
            "currency": "EUR",
            "note": "precio de estantería",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Money is serialized as strings; the price is manual and exact-store scoped.
    assert body["amount"] == "1.49"
    assert body["currency"] == "EUR"
    assert body["price_type"] == "manual"
    assert body["price_scope"] == "exact_store"
    assert body["store_id"] == str(store.public_id)
    assert isinstance(body["confidence_score"], str)

    # An append-only manual PriceObservation was recorded (open interval, not disputed).
    obs = (
        db_session.execute(
            select(PriceObservation).where(
                PriceObservation.retailer_id == retailer.id,
                PriceObservation.price_type == "manual",
            )
        )
        .scalars()
        .all()
    )
    assert len(obs) == 1
    assert obs[0].valid_until is None
    assert obs[0].store_id == store.id
    assert str(obs[0].public_id) == body["id"]

    # It was projected into ProductPrice so the meal-plan engine sees it.
    projected = (
        db_session.execute(
            select(ProductPrice).where(ProductPrice.store_id == store.id)
        )
        .scalars()
        .all()
    )
    assert len(projected) == 1
    assert projected[0].amount == obs[0].amount


def test_manual_price_for_existing_product(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    product = Product(
        retailer_id=retailer.id,
        external_id="SKU-1",
        name="Aceite 1 L",
        is_synthetic=False,
    )
    db_session.add(product)
    db_session.flush()

    resp = client.post(
        "/api/v1/admin/prices/manual",
        headers=csrf(token),
        json={
            "retailer_code": retailer.slug,
            "product_id": str(product.public_id),
            "amount": "5.95",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # No store supplied -> honest national scope, never exact_store.
    assert body["price_scope"] == "national"
    assert body["store_id"] is None
    assert body["price_type"] == "manual"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_manual_price_rejects_non_positive_amount(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)

    resp = client.post(
        "/api/v1/admin/prices/manual",
        headers=csrf(token),
        json={"retailer_code": retailer.slug, "barcode": "8410000000001", "amount": "0"},
    )
    assert resp.status_code == 422, resp.text
    assert "amount" in resp.text.lower()


def test_manual_price_rejects_exact_store_without_store(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)

    resp = client.post(
        "/api/v1/admin/prices/manual",
        headers=csrf(token),
        json={
            "retailer_code": retailer.slug,
            "barcode": "8410000000001",
            "amount": "1.50",
            "price_scope": "exact_store",
        },
    )
    assert resp.status_code == 422, resp.text


def test_manual_price_unknown_retailer_404(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    resp = client.post(
        "/api/v1/admin/prices/manual",
        headers=csrf(token),
        json={"retailer_code": "does-not-exist", "barcode": "x", "amount": "1.50"},
    )
    assert resp.status_code == 404, resp.text
