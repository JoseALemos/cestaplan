"""Auth flow tests: register, login, session cookie, /me, logout, rate limit."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.deps import SESSION_COOKIE_NAME
from cestaplan_api.models import User, UserSession

from .conftest import csrf, login, register


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


def test_register_login_me_logout_flow(client: TestClient) -> None:
    email = _email()
    register(client, email)

    token = login(client, email)
    assert SESSION_COOKIE_NAME in client.cookies

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email

    out = client.post("/api/v1/auth/logout", headers=csrf(token))
    assert out.status_code == 200

    # After logout the session is revoked server-side; /me must fail.
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


def test_duplicate_email_rejected(client: TestClient) -> None:
    email = _email()
    register(client, email)
    resp = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "another-strong-pass"}
    )
    assert resp.status_code == 409


def test_email_normalised_lowercase(client: TestClient) -> None:
    email = _email().upper()
    register(client, email)
    # Login with the lowercase form succeeds -> stored normalised.
    token = login(client, email.lower())
    assert token


def test_wrong_password_rejected(client: TestClient) -> None:
    email = _email()
    register(client, email)
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "totally-wrong-pass"}
    )
    assert resp.status_code == 401


def test_login_rate_limit_trips(client: TestClient) -> None:
    email = _email()
    register(client, email)
    # Limiter is 5 failures / window; the 6th attempt is throttled.
    for _ in range(5):
        r = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "bad-password-x"}
        )
        assert r.status_code == 401
    throttled = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "bad-password-x"}
    )
    assert throttled.status_code == 429


def test_logout_requires_csrf(client: TestClient) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    # No CSRF header -> rejected even with a valid session cookie.
    assert client.post("/api/v1/auth/logout").status_code == 403


def test_expired_session_rejected(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)

    user = db_session.execute(select(User).where(User.email == email)).scalar_one()
    session = db_session.execute(
        select(UserSession).where(UserSession.user_id == user.id)
    ).scalar_one()
    session.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_revoked_session_rejected(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)

    user = db_session.execute(select(User).where(User.email == email)).scalar_one()
    session = db_session.execute(
        select(UserSession).where(UserSession.user_id == user.id)
    ).scalar_one()
    session.revoked_at = datetime.now(UTC)
    db_session.commit()

    assert client.get("/api/v1/auth/me").status_code == 401


def test_password_recovery_is_uniform_stub(client: TestClient) -> None:
    # Unknown email still returns 200 with a generic message (no account disclosure).
    resp = client.post(
        "/api/v1/auth/password-recovery", json={"email": _email()}
    )
    assert resp.status_code == 200
    assert "detail" in resp.json()
