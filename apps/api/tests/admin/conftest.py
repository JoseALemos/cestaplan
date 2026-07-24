"""Fixtures for the admin import + source tests.

Same transactional-isolation strategy as ``tests/api``: each test runs inside one DB
transaction rolled back on teardown. The FastAPI ``get_db`` dependency is overridden to
reuse the test's savepoint session, and the app mounts the auth + admin routers.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import engine, get_db
from cestaplan_api.deps import CSRF_HEADER_NAME
from cestaplan_api.models import User
from cestaplan_api.routers import admin, admin_mappings, auth
from cestaplan_api.security import login_rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    login_rate_limiter.reset_all()
    yield
    login_rate_limiter.reset_all()


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
    app.include_router(admin.router)
    app.include_router(admin_mappings.router)

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
    resp = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


def login(client: TestClient, email: str, password: str = "correct-horse-battery") -> str:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["csrf_token"]


def csrf(token: str) -> dict[str, str]:
    return {CSRF_HEADER_NAME: token}


def promote_to_admin(db_session: Session, email: str) -> None:
    """Flip ``is_admin`` for the user (mirrors ``make_admin`` on the shared session)."""
    user = db_session.execute(
        select(User).where(User.email == email.lower())
    ).scalar_one()
    user.is_admin = True
    db_session.flush()
