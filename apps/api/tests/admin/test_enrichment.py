"""Enrichment service + admin endpoint tests — HTTPX mocked, NO network.

Covers: applying OFF data idempotently (barcode + nutrition; run twice -> no duplicates),
prices left untouched, the "no matching product" decision, the OFF-disabled gate (409),
admin authz + CSRF, and the dry barcode lookup endpoint.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openfoodfacts import OpenFoodFactsAdapter
from cestaplan_api.models import (
    Product,
    ProductBarcode,
    ProductNutrition,
    ProductPrice,
    Retailer,
)
from cestaplan_api.services import enrichment

from .conftest import csrf, login, promote_to_admin, register

_BARCODE = "3017620422003"
_PAYLOAD = {
    "status": 1,
    "product": {
        "product_name": "Crema de avellanas",
        "brands": "MarcaX",
        "categories_tags": ["en:spreads", "en:hazelnut-spreads"],
        "ingredients_text": "Azúcar, avellanas, leche",
        "allergens_tags": ["en:milk", "en:nuts"],
        "traces_tags": ["en:peanuts"],
        "nutriments": {
            "energy-kcal_100g": 539,
            "proteins_100g": 6.3,
            "carbohydrates_100g": 57.5,
            "fat_100g": 30.9,
            "fiber_100g": 0,
            "salt_100g": 0.107,
        },
        "image_url": "https://images.openfoodfacts.org/front.jpg",
        "price": "9.99",  # planted noise; must never be read/stored
    },
}


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}@example.com"


def _off_adapter(payload: dict | None = None, *, status_code: int = 200) -> OpenFoodFactsAdapter:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload if payload is not None else _PAYLOAD)

    return OpenFoodFactsAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _make_product(db: Session, *, with_barcode: str | None = _BARCODE) -> Product:
    retailer = Retailer(
        slug=f"acme-{uuid.uuid4().hex[:8]}",
        name="Acme",
        adapter_key="csv",
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    product = Product(
        retailer_id=retailer.id,
        external_id=f"P-{uuid.uuid4().hex[:8]}",
        name="Producto sin enriquecer",
        is_synthetic=False,
    )
    db.add(product)
    db.flush()
    if with_barcode:
        db.add(ProductBarcode(product_id=product.id, barcode=with_barcode, is_primary=True))
        db.flush()
    return product


# --------------------------------------------------------------------------- #
# Service: apply, idempotency, prices untouched
# --------------------------------------------------------------------------- #
def test_apply_writes_nutrition_and_updates_product(db_session: Session) -> None:
    product = _make_product(db_session)
    result = enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=True, adapter=_off_adapter()
    )
    assert result.status == "applied"
    assert result.applied is True
    assert result.matched_products == 1

    db_session.refresh(product)
    assert product.brand == "MarcaX"
    assert product.image_url == "https://images.openfoodfacts.org/front.jpg"
    assert product.category_code == "hazelnut-spreads"

    nutr = db_session.execute(
        select(ProductNutrition).where(ProductNutrition.product_id == product.id)
    ).scalar_one()
    assert nutr.source_type == "open_dataset"
    assert nutr.source_url == f"https://world.openfoodfacts.org/product/{_BARCODE}"
    assert set(nutr.allergens or []) == {"milk", "tree_nut"}
    assert set(nutr.traces or []) == {"peanut"}
    assert nutr.energy_kcal is not None


def test_apply_is_idempotent(db_session: Session) -> None:
    product = _make_product(db_session)

    def counts() -> tuple[int, int, int]:
        bc = db_session.execute(
            select(func.count(ProductBarcode.id)).where(
                ProductBarcode.product_id == product.id
            )
        ).scalar_one()
        nut = db_session.execute(
            select(func.count(ProductNutrition.id)).where(
                ProductNutrition.product_id == product.id
            )
        ).scalar_one()
        pr = db_session.execute(
            select(func.count(ProductPrice.id)).where(
                ProductPrice.product_id == product.id
            )
        ).scalar_one()
        return bc, nut, pr

    enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=True, adapter=_off_adapter()
    )
    first = counts()
    enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=True, adapter=_off_adapter()
    )
    second = counts()

    assert first == second == (1, 1, 0)  # one barcode, one nutrition row, ZERO prices


def test_no_matching_product_writes_nothing(db_session: Session) -> None:
    # A barcode nobody in the catalogue carries.
    result = enrichment.enrich_product_by_barcode(
        db_session, "9999999999999", apply=True, adapter=_off_adapter()
    )
    assert result.status == "no_product"
    assert result.applied is False
    # OFF data still returned for reference, but nothing persisted.
    assert result.product is not None


def test_dry_lookup_writes_nothing(db_session: Session) -> None:
    product = _make_product(db_session)
    result = enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=False, adapter=_off_adapter()
    )
    assert result.status == "found"
    nut = db_session.execute(
        select(func.count(ProductNutrition.id)).where(
            ProductNutrition.product_id == product.id
        )
    ).scalar_one()
    assert nut == 0


def test_not_found_is_graceful(db_session: Session) -> None:
    _make_product(db_session)
    result = enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=True, adapter=_off_adapter(status_code=404)
    )
    assert result.status == "not_found"
    assert result.applied is False


def test_disabled_source_refuses(db_session: Session) -> None:
    ds = enrichment.ensure_off_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()
    result = enrichment.enrich_product_by_barcode(
        db_session, _BARCODE, apply=True, adapter=_off_adapter()
    )
    assert result.status == "disabled"


def test_enrich_specific_product_uses_its_barcode(db_session: Session) -> None:
    product = _make_product(db_session)
    result = enrichment.enrich_product(db_session, product, adapter=_off_adapter())
    assert result.status == "applied"
    assert result.product_public_id == str(product.public_id)


def test_enrich_product_without_barcode(db_session: Session) -> None:
    product = _make_product(db_session, with_barcode=None)
    result = enrichment.enrich_product(db_session, product, adapter=_off_adapter())
    assert result.status == "no_barcode"


# --------------------------------------------------------------------------- #
# Admin endpoints: authz, csrf, dry lookup, apply, disabled gate
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _patch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the endpoints use a mocked OFF adapter (no network)."""
    monkeypatch.setattr(enrichment, "OpenFoodFactsAdapter", lambda: _off_adapter())


def test_enrich_barcode_requires_admin(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post(
        "/api/v1/admin/enrich/barcode", json={"barcode": _BARCODE}, headers=csrf(token)
    )
    assert resp.status_code == 403


def test_enrich_barcode_requires_csrf(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post("/api/v1/admin/enrich/barcode", json={"barcode": _BARCODE})
    assert resp.status_code == 403


def test_enrich_barcode_dry_lookup(
    client: TestClient, db_session: Session, _patch_off: None
) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post(
        "/api/v1/admin/enrich/barcode", json={"barcode": _BARCODE}, headers=csrf(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["applied"] is False
    assert body["off_product"]["allergens"] == ["milk", "tree_nut"]
    assert "ODbL" in body["license_code"]
    assert "Open Food Facts" in body["attribution"]
    # No price surfaced anywhere.
    import json as _json

    assert "9.99" not in _json.dumps(body)


def test_enrich_barcode_disabled_returns_409(
    client: TestClient, db_session: Session, _patch_off: None
) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    ds = enrichment.ensure_off_data_source(db_session)
    ds.is_enabled = False
    db_session.flush()
    resp = client.post(
        "/api/v1/admin/enrich/barcode", json={"barcode": _BARCODE}, headers=csrf(token)
    )
    assert resp.status_code == 409


def test_product_enrich_applies(
    client: TestClient, db_session: Session, _patch_off: None
) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    product = _make_product(db_session)

    resp = client.post(
        f"/api/v1/admin/products/{product.public_id}/enrich",
        json={},
        headers=csrf(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["product_public_id"] == str(product.public_id)

    db_session.refresh(product)
    assert product.brand == "MarcaX"


def test_product_enrich_unknown_product_404(
    client: TestClient, db_session: Session, _patch_off: None
) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post(
        f"/api/v1/admin/products/{uuid.uuid4()}/enrich", json={}, headers=csrf(token)
    )
    assert resp.status_code == 404


def test_product_enrich_requires_admin(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    product = _make_product(db_session)
    resp = client.post(
        f"/api/v1/admin/products/{product.public_id}/enrich",
        json={},
        headers=csrf(token),
    )
    assert resp.status_code == 403


def test_sources_lists_openfoodfacts(
    client: TestClient, db_session: Session
) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)
    resp = client.get("/api/v1/admin/sources")
    assert resp.status_code == 200
    by_key = {row["adapter_key"]: row for row in resp.json()}
    off = by_key["openfoodfacts"]
    assert off["source_type"] == "open_dataset"
    assert off["enabled"] is True
    assert off["requires_network"] is True
    assert off["license_code"] == "ODbL"
    assert "Open Food Facts" in off["attribution_text"]
    assert off["capabilities"]["get_price"] is False
