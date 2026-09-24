"""Fixtures for FASE 5 cloud metering + quota tests.

Same live-Postgres, transactional-rollback model as the other suites: the demo
catalogue is seeded once (committed) so the DB->engine adapter has data; each test runs
inside a transaction rolled back on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.security import (
    login_rate_limiter,
    plan_generation_rate_limiter,
    registration_rate_limiter,
)

# _ensure_demo_seed (siembra del catálogo demo) vive ahora en el conftest RAÍZ.


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
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
