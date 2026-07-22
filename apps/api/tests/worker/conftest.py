"""Fixtures for worker / plan-generation tests.

Tests run against the live local Postgres (``DATABASE_URL``). The demo catalogue +
recipe book is seeded once (committed) so the DB->engine adapter has data to read;
each test runs inside a transaction rolled back on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import Recipe
from cestaplan_api.scripts.seed_demo import main as seed_demo_main


@pytest.fixture(scope="session", autouse=True)
def _ensure_demo_seed() -> None:
    """Seed the demo catalogue once per session if the DB has no recipes."""
    with Session(bind=engine) as check:
        count = check.scalar(select(func.count()).select_from(Recipe))
    if not count:
        seed_demo_main()


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
