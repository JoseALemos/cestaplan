"""Regression: the onboarding-finalization write (POST /households) enforces CSRF correctly.

Locks the contract diagnosed for the cloud 403: the create-household route runs the CSRF
double-submit check before auth. A request without the CSRF header is rejected with
403 "Token CSRF ausente"; a mismatched header is "Token CSRF inválido"; a valid session +
matching CSRF header succeeds and makes the caller the owner + first member.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db

from .conftest import csrf, login, register


def _client(db_session: Session) -> TestClient:
    from cestaplan_api.routers import auth, households

    app = FastAPI()
    for module in (auth, households):
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


def _email() -> str:
    return f"onb-{uuid.uuid4().hex[:12]}@example.com"


def test_create_household_succeeds_with_session_and_csrf_header(db_session: Session) -> None:
    client = _client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post(
        "/api/v1/households", json={"name": "Casa", "currency": "EUR"}, headers=csrf(token)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["my_role"] == "owner"
    assert body["member_count"] == 1


def test_create_household_without_csrf_header_is_403_ausente(db_session: Session) -> None:
    client = _client(db_session)
    email = _email()
    register(client, email)
    login(client, email)  # session cookie present, but no CSRF header sent
    resp = client.post("/api/v1/households", json={"name": "Casa", "currency": "EUR"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token CSRF ausente"


def test_create_household_with_mismatched_csrf_is_403_invalido(db_session: Session) -> None:
    client = _client(db_session)
    email = _email()
    register(client, email)
    login(client, email)
    resp = client.post(
        "/api/v1/households",
        json={"name": "Casa", "currency": "EUR"},
        headers=csrf("not-the-real-token"),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token CSRF inválido"


def test_finalization_makes_caller_owner_and_member(db_session: Session) -> None:
    """The despensa/household context depends on the caller becoming an owner MEMBER, not just
    Household.owner_user_id — this is why an authorized creation resolves '/despensa'."""
    client = _client(db_session)
    email = _email()
    register(client, email)
    token = login(client, email)
    created = client.post(
        "/api/v1/households", json={"name": "Casa", "currency": "EUR"}, headers=csrf(token)
    ).json()
    listed = client.get("/api/v1/households")
    assert listed.status_code == 200
    ids = [h["id"] for h in listed.json()]
    assert created["id"] in ids  # visible to the owner via membership, no logout needed
