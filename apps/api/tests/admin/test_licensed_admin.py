"""Licensed-catalog admin API (FASE 4): field maps, sample import, review queue, coverage.

Exercises the HTTP surface end to end: admin+CSRF gating, a commit sample-import that
produces machine candidates, and the review workflow (approve -> human_verified+active,
reject -> disputed+inactive) plus the coverage metrics that gate FASE 5.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.deps import CSRF_HEADER_NAME
from cestaplan_api.models import Retailer, User
from cestaplan_api.routers import auth, licensed_admin
from tests.fixtures.provider_scenarios import seed_test_canonical_ingredients

_CSV = (
    "sku,name,price,currency,qty,unit\n"
    "LIC-001,Leche desnatada brick 1 L,0.88,EUR,1000,ml\n"
    "LIC-002,Garbanzos cocidos bote 400 g,0.91,EUR,400,g\n"
    "LIC-003,Vinagre de vino 750 ml,0.87,EUR,750,ml\n"
)
_FIELD_MAP = json.dumps(
    {
        "field_map": {
            "external_id": "sku",
            "product_name": "name",
            "amount": "price",
            "currency": "currency",
            "package_quantity": "qty",
            "package_unit": "unit",
        },
        "default_currency": "EUR",
    }
)


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(licensed_admin.router)

    def _override_get_db() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _register_login_admin(client: TestClient, db_session: Session, email: str) -> str:
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
        ).status_code
        == 201
    )
    user = db_session.execute(select(User).where(User.email == email.lower())).scalar_one()
    user.is_admin = True
    db_session.flush()
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def _make_retailer(db_session: Session, slug: str) -> str:
    r = Retailer(slug=slug, name="Licensed Chain", adapter_key="feed", is_synthetic=False)
    db_session.add(r)
    db_session.flush()
    return str(r.public_id)


def test_review_queue_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/admin/licensed/review-queue").status_code == 401


def test_non_admin_forbidden(client: TestClient, db_session: Session) -> None:
    assert (
        client.post(
            "/api/v1/auth/register", json={"email": "u@x.com", "password": "correct-horse-battery"}
        ).status_code
        == 201
    )
    client.post(
        "/api/v1/auth/login", json={"email": "u@x.com", "password": "correct-horse-battery"}
    )
    assert client.get("/api/v1/admin/licensed/review-queue").status_code == 403


def test_field_mapping_crud(client: TestClient, db_session: Session) -> None:
    token = _register_login_admin(client, db_session, "fm@x.com")
    headers = {CSRF_HEADER_NAME: token}
    resp = client.post(
        "/api/v1/admin/licensed/field-mappings",
        json={"source_name": "prov-a", "field_map": {"external_id": "sku"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    # duplicate source_name -> 409
    dup = client.post(
        "/api/v1/admin/licensed/field-mappings",
        json={"source_name": "prov-a", "field_map": {"external_id": "sku"}},
        headers=headers,
    )
    assert dup.status_code == 409
    listed = client.get("/api/v1/admin/licensed/field-mappings")
    assert any(m["source_name"] == "prov-a" for m in listed.json())


def test_sample_import_review_and_coverage_flow(client: TestClient, db_session: Session) -> None:
    seed_test_canonical_ingredients(db_session)  # hermetic: the 75 canonical ingredients
    token = _register_login_admin(client, db_session, "admin@x.com")
    headers = {CSRF_HEADER_NAME: token}
    retailer_id = _make_retailer(db_session, "lic-http")

    # 1) commit sample-import -> machine candidates
    resp = client.post(
        "/api/v1/admin/licensed/sample-import",
        files={"file": ("catalogo.csv", _CSV, "text/csv")},
        data={"retailer_id": retailer_id, "field_map_json": _FIELD_MAP, "dry_run": "false"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["ok"] is True
    assert report["dry_run"] is False
    assert report["coverage"]["costable_products"] == 3
    assert report["mapping"]["products_matched"] >= 2

    # 2) review queue lists the pending candidates
    queue = client.get(f"/api/v1/admin/licensed/review-queue?retailer_id={retailer_id}").json()
    assert len(queue) >= 2
    assert all(c["verification_status"] == "machine_verified" for c in queue)

    # 3) approve the first, reject the second
    approve = client.post(
        f"/api/v1/admin/licensed/review/{queue[0]['id']}/approve", headers=headers
    )
    assert approve.status_code == 200
    assert approve.json()["verification_status"] == "human_verified"
    assert approve.json()["is_active"] is True

    reject = client.post(f"/api/v1/admin/licensed/review/{queue[1]['id']}/reject", headers=headers)
    assert reject.status_code == 200
    assert reject.json()["verification_status"] == "disputed"
    assert reject.json()["is_active"] is False

    # approved candidate leaves the pending queue
    queue_after = client.get(
        f"/api/v1/admin/licensed/review-queue?retailer_id={retailer_id}"
    ).json()
    assert queue[0]["id"] not in {c["id"] for c in queue_after}

    # 4) coverage reflects the one human-verified ingredient
    cov = client.get("/api/v1/admin/licensed/coverage").json()
    mine = next(c for c in cov if c["retailer_id"] == retailer_id)
    assert mine["costable_variants"] == 3
    assert mine["verified_ingredients"] == 1
    assert mine["ingredients_total"] == 75

    # mutating endpoints demand CSRF
    assert client.post(f"/api/v1/admin/licensed/review/{queue[0]['id']}/approve").status_code == 403
