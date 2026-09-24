"""Pytest fixtures for the auth + household API tests.

Connection: tests run against the live local Postgres pointed to by ``DATABASE_URL``
(repo-root ``.env``; ``postgresql+psycopg://cestaplan:cestaplan@localhost:5432/cestaplan``
by default). Each test runs inside a single database transaction that is rolled back on
teardown, so tests are isolated and leave no data behind — the schema is the one created
by the Alembic migration; tests never create or drop tables.

The FastAPI ``get_db`` dependency is overridden to reuse the test's transactional
session (``join_transaction_mode="create_savepoint"`` turns the app's per-request
``commit`` into a savepoint release inside the outer transaction).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cestaplan_api.db import engine, get_db
from cestaplan_api.deps import CSRF_HEADER_NAME
from cestaplan_api.routers import auth, catalog, households, invitations, pantry, prices
from cestaplan_api.security import (
    login_rate_limiter,
    plan_generation_rate_limiter,
    registration_rate_limiter,
)

# La siembra del catálogo demo (_ensure_demo_seed) vive ahora en el conftest RAÍZ, heredada por
# todos los paquetes (antes estaba duplicada aquí y en cloud/worker; ver tests/conftest.py).


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    """Keep the in-memory rate limiters from leaking attempts across tests.

    TestClient always presents the same client IP, so per-IP counters would otherwise
    accumulate across the whole session; reset every limiter before and after each test.
    """
    for limiter in (
        login_rate_limiter,
        registration_rate_limiter,
        plan_generation_rate_limiter,
    ):
        limiter.reset_all()
    yield
    for limiter in (
        login_rate_limiter,
        registration_rate_limiter,
        plan_generation_rate_limiter,
    ):
        limiter.reset_all()


@pytest.fixture()
def db_session() -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(households.router)
    app.include_router(invitations.router)
    app.include_router(pantry.router)
    app.include_router(catalog.router)
    app.include_router(prices.router)

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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def register(client: TestClient, email: str, password: str = "correct-horse-battery") -> dict:
    resp = client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client: TestClient, email: str, password: str = "correct-horse-battery") -> str:
    """Log in and return the CSRF token (cookies are stored on the client)."""
    resp = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def csrf(token: str) -> dict[str, str]:
    return {CSRF_HEADER_NAME: token}
