"""Fixtures for DB-backed service tests.

Tests run against the live local Postgres (``DATABASE_URL``); each test runs inside a single
transaction rolled back on teardown, so it is isolated and leaves no data behind. Service tests
that build their own hermetic scenario (retailers, ingredients, recipes) need only this session.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.db import engine


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
