"""Rate limiting general por IP: la dependencia reutilizable y su aplicación al registro."""

from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cestaplan_api.deps import rate_limit
from cestaplan_api.security import RateLimiter, registration_rate_limiter


def _email() -> str:
    return f"rl-{uuid.uuid4().hex[:12]}@example.com"


def test_rate_limiter_counts_and_resets() -> None:
    limiter = RateLimiter(max_attempts=2, window_seconds=60)
    assert limiter.is_limited("k") is False
    limiter.record("k")
    assert limiter.is_limited("k") is False
    limiter.record("k")
    assert limiter.is_limited("k") is True
    # Otra clave (otra IP) no se ve afectada.
    assert limiter.is_limited("other") is False
    limiter.reset("k")
    assert limiter.is_limited("k") is False


def test_rate_limit_dependency_returns_429_over_limit() -> None:
    app = FastAPI()
    limiter = RateLimiter(max_attempts=2, window_seconds=60)

    @app.get("/thing", dependencies=[Depends(rate_limit(limiter))])
    def thing() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 200
    resp = client.get("/thing")
    assert resp.status_code == 429
    assert "detail" in resp.json()


def test_registration_endpoint_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bajamos el umbral para no depender del límite productivo (10/hora).
    monkeypatch.setattr(registration_rate_limiter, "max_attempts", 3)
    for _ in range(3):
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": _email(), "password": "correct-horse-battery"},
        )
        assert resp.status_code == 201, resp.text
    # La 4ª desde la misma IP se corta con 429 (antes de crear la cuenta).
    over = client.post(
        "/api/v1/auth/register",
        json={"email": _email(), "password": "correct-horse-battery"},
    )
    assert over.status_code == 429
