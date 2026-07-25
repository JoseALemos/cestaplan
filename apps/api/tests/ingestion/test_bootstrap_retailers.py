"""Canonical retailer bootstrap: authorized chains only, idempotent, never a product/price row."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice, Retailer
from cestaplan_api.tools import bootstrap_retailers as boot
from cestaplan_api.tools.bootstrap_retailers import bootstrap

TEST_SLUG = "bootstrap_test_chain"


def _counts(db: Session) -> tuple[int, int]:
    return (
        int(db.scalar(select(func.count()).select_from(Product)) or 0),
        int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0),
    )


def test_bootstrap_creates_real_retailer_and_is_idempotent(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hermetic slug not present in ambient data, so we can assert creation deterministically.
    monkeypatch.setitem(boot.AUTHORIZED_CHAINS, TEST_SLUG, "Bootstrap Test Chain")
    before = _counts(db_session)

    created = bootstrap(db_session, [TEST_SLUG])
    assert created == [TEST_SLUG]
    retailer = db_session.execute(
        select(Retailer).where(Retailer.slug == TEST_SLUG)
    ).scalar_one()
    assert retailer.name == "Bootstrap Test Chain"
    assert retailer.is_synthetic is False
    assert retailer.adapter_key == f"parsebot-{TEST_SLUG}"
    assert retailer.country == "ES"

    # Second run creates nothing, and no product/price rows were ever touched.
    assert bootstrap(db_session, [TEST_SLUG]) == []
    assert _counts(db_session) == before


def test_bootstrap_refuses_unauthorized_chain(db_session: Session) -> None:
    with pytest.raises(ValueError, match="not authorized"):
        bootstrap(db_session, ["mercadona"])


def test_alcampo_is_authorized() -> None:
    assert boot.AUTHORIZED_CHAINS.get("alcampo") == "Alcampo"
