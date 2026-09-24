"""Fixtures for worker / plan-generation tests.

Tests run against the live local Postgres (``DATABASE_URL``). The demo catalogue +
recipe book is seeded once (committed) so the DB->engine adapter has data to read;
each test runs inside a transaction rolled back on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.db import engine

# _ensure_demo_seed (siembra del catálogo demo) vive ahora en el conftest RAÍZ.


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
