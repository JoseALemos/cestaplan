"""Admin promotion endpoints: auth + gate (409) + dry-run write nothing.

The service logic is covered in tests/ingestion/test_provider_promotion.py; here we check the HTTP
surface: admin-only, a blocked gate returns 409 with typed reasons, and a dry-run promotion writes
no productive rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.db import get_db
from cestaplan_api.deps import CSRF_HEADER_NAME
from cestaplan_api.models import PriceObservation, ProductPrice, User
from cestaplan_api.routers import auth, provider_promotion_admin
from tests.fixtures.provider_scenarios import (
    ensure_test_ingredient,
    seed_test_catalog_product,
    seed_test_mapping_candidate,
    seed_test_provider_activation,
    seed_test_retailer,
    seed_test_store,
)

PROVIDER = "test_provider"
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(provider_promotion_admin.router)

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


def _admin(client: TestClient, db_session: Session) -> dict[str, str]:
    email = "promo-admin@example.com"
    resp = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 201, resp.text
    db_session.execute(select(User).where(User.email == email)).scalar_one().is_admin = True
    db_session.flush()
    token = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    ).json()["csrf_token"]
    return {CSRF_HEADER_NAME: token}


def _ready_scenario(db: Session) -> None:
    """Prerequisites met (not approved yet) + one approved candidate + a staging price."""
    seed_test_provider_activation(
        db,
        PROVIDER,
        transport_status="operational",
        mapper_status="verified",
        data_quality_status="accepted",
        data_rights_status="commercial_use_allowed",
    )
    retailer = seed_test_retailer(db, PROVIDER)
    store = seed_test_store(db, retailer)
    ingredient = ensure_test_ingredient(db, "aceite_de_oliva")
    product, variant = seed_test_catalog_product(db, retailer, "OP-1", name="Aceite 1L")
    db.add(
        PriceObservation(
            retailer_id=retailer.id,
            store_id=store.id,
            product_variant_id=variant.id,
            price_scope="national",
            price_type="regular",
            amount=Decimal("4.19"),
            currency="EUR",
            observed_at=NOW,
            imported_at=NOW,
            valid_from=NOW,
            confidence_score=Decimal("1.0"),
            staging_only=True,
        )
    )
    seed_test_mapping_candidate(
        db,
        PROVIDER,
        ingredient,
        "OP-1",
        retailer_slug=PROVIDER,
        mapping_status="manually_approved",
        active=True,
        normalized_product_id=product.id,
    )
    db.flush()


def _prices(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0)


def test_endpoints_require_admin(client: TestClient) -> None:
    # No session at all -> not authorized (401/403), never a 200.
    assert client.get(f"/api/v1/admin/providers/{PROVIDER}/promotion-status").status_code in (
        401,
        403,
    )


def test_promote_blocked_returns_409_with_reasons(client: TestClient, db_session: Session) -> None:
    headers = _admin(client, db_session)
    _ready_scenario(db_session)  # prerequisites met but NOT approved
    before = _prices(db_session)
    resp = client.post(f"/api/v1/admin/providers/{PROVIDER}/promote", headers=headers)
    assert resp.status_code == 409, resp.text
    assert "reasons" in resp.json()["detail"]
    assert _prices(db_session) == before  # nothing written


def test_approve_then_dry_run_promotion_writes_nothing(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cestaplan_api.services import provider_promotion

    # Providers are disabled by default; enable them for this end-to-end flow.
    monkeypatch.setattr(
        provider_promotion, "get_settings", lambda: Settings(price_providers_enabled=True)
    )
    headers = _admin(client, db_session)
    _ready_scenario(db_session)

    # Approve via the real endpoint (records the admin as the approver).
    approved = client.post(
        f"/api/v1/admin/providers/{PROVIDER}/production-approval", headers=headers
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["production_approved"] is True
    assert approved.json()["production_approved_by"] is not None

    before = _prices(db_session)
    resp = client.post(
        f"/api/v1/admin/providers/{PROVIDER}/promote",
        params={"dry_run": "true"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["mappings_created"] == 1  # exact preview counts
    assert body["prices_written"] == 1
    assert _prices(db_session) == before  # ...but nothing persisted
