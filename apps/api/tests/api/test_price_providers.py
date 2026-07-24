"""API tests for GET /api/v1/price-providers — authorized rights + honest separation of axes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db

from .conftest import login, register


def _client(db_session: Session) -> TestClient:
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


def _providers(db_session: Session) -> dict[str, dict]:
    client = _client(db_session)
    email = f"pp-{uuid.uuid4().hex[:12]}@example.com"
    register(client, email)
    login(client, email)
    resp = client.get("/api/v1/price-providers")
    assert resp.status_code == 200, resp.text
    return {row["provider"]: row for row in resp.json()}


def test_price_providers_requires_auth(db_session: Session) -> None:
    assert _client(db_session).get("/api/v1/price-providers").status_code == 401


def test_seven_external_chains_are_authorized_not_official_api(db_session: Session) -> None:
    providers = _providers(db_session)
    for code in (
        "parsebot-dia",
        "parsebot-alcampo",
        "parsebot-carrefour",
        "parsebot-lidl",
        "parsebot-aldi",
        "parsebot-deza",
        "apify-mercadona",
    ):
        p = providers[code]
        assert p["authorized_source"] is True, code
        assert p["data_rights_status"] == "commercial_use_allowed", code
        assert p["license_display_name"] == "Licencia comercial privada", code
        assert p["rights_display_name"] == "Uso autorizado", code
        # The chain owner authorized the data — but the intermediary is NOT an official API.
        assert p["official_api"] is False, code
        assert p["technical_provider"] in ("Parse.bot", "Apify"), code
        # attribution governed by a private agreement -> null (not "not required").
        assert p["attribution_required"] is None, code


def test_open_prices_and_demo_keep_their_own_basis(db_session: Session) -> None:
    providers = _providers(db_session)
    op = providers["open-prices"]
    assert op["data_rights_status"] == "odbl"
    assert op["official_api"] is True
    assert op["attribution_required"] is True
    demo = providers["demo"]
    assert demo["data_rights_status"] == "own_synthetic"
    assert demo["official_api"] is False
    assert demo["attribution_required"] is False


def test_rights_never_enable_production(db_session: Session) -> None:
    for p in _providers(db_session).values():
        assert p["production_enabled"] is False
        assert p["production_approved"] is False
        assert p["production_eligibility"] is False


def test_no_internal_fields_are_ever_exposed(db_session: Session) -> None:
    client = _client(db_session)
    email = f"pp-{uuid.uuid4().hex[:12]}@example.com"
    register(client, email)
    login(client, email)
    raw = client.get("/api/v1/price-providers").text
    for secret in (
        "internal_evidence_reference",
        "legal_notes_internal",
        "authorization_verified_by",
    ):
        assert secret not in raw


def test_available_fields_reflect_declared_capabilities(db_session: Session) -> None:
    p = _providers(db_session)["parsebot-dia"]
    # Declared capabilities, not invented data.
    assert "prices" in p["available_fields"]
    assert isinstance(p["available_fields"], list)
