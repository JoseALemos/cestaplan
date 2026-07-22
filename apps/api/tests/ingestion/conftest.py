"""Fixtures for the price-ingestion model + contract tests.

Same transactional-isolation strategy as ``tests/api``: each test runs inside one DB
transaction that is rolled back on teardown (``join_transaction_mode="create_savepoint"``),
so tests are isolated and leave no data behind. No network is ever touched.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from cestaplan_api.db import engine
from cestaplan_api.models import ExternalProduct, ProductVariant, Retailer, Store


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
def variant(db_session: Session) -> ProductVariant:
    """Create a synthetic retailer -> store -> external product -> variant chain.

    Returns the :class:`ProductVariant` a price observation can point at. Everything is
    created inside the test transaction and rolled back on teardown.
    """
    retailer = Retailer(
        slug="test-ingest-retailer",
        name="Test Ingest Retailer",
        adapter_key="test",
        is_synthetic=True,
    )
    db_session.add(retailer)
    db_session.flush()

    store = Store(retailer_id=retailer.id, name="Test Store", is_synthetic=True)
    db_session.add(store)

    external = ExternalProduct(retailer_id=retailer.id, external_id="EXT-1")
    db_session.add(external)
    db_session.flush()

    pv = ProductVariant(
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name="Test Variant 1kg",
    )
    db_session.add(pv)
    db_session.flush()
    return pv
