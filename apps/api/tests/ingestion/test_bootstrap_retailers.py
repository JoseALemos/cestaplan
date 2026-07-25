"""Canonical retailer bootstrap: seven authorized chains, idempotent, never a product/price row."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import Product, ProductPrice, Retailer
from cestaplan_api.tools import bootstrap_retailers as boot
from cestaplan_api.tools.bootstrap_retailers import _adapter_key, _resolve_slugs, bootstrap

TEST_SLUG = "bootstrap_test_chain"

EXPECTED_ADAPTER_KEYS = {
    "alcampo": "parsebot-alcampo",
    "dia": "parsebot-dia",
    "carrefour": "parsebot-carrefour",
    "lidl": "parsebot-lidl",
    "aldi": "parsebot-aldi",
    "deza": "parsebot-deza",
    "mercadona": "apify-mercadona",  # NOT parsebot-mercadona — resolved from the matrix
}


def _counts(db: Session) -> tuple[int, int]:
    return (
        int(db.scalar(select(func.count()).select_from(Product)) or 0),
        int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0),
    )


def test_seven_chains_authorized_with_expected_adapter_keys() -> None:
    assert set(boot.AUTHORIZED_CHAINS) == set(EXPECTED_ADAPTER_KEYS)
    for slug, provider_code in EXPECTED_ADAPTER_KEYS.items():
        assert _adapter_key(slug) == provider_code


def test_resolve_slugs_all_and_by_provider() -> None:
    assert _resolve_slugs(all_chains=True, provider=None) == sorted(boot.AUTHORIZED_CHAINS)
    assert _resolve_slugs(all_chains=False, provider="parsebot-dia") == ["dia"]
    assert _resolve_slugs(all_chains=False, provider="apify-mercadona") == ["mercadona"]
    # open-prices is a cross-cutting source, never an authorized chain retailer.
    with pytest.raises(ValueError, match="not an authorized chain"):
        _resolve_slugs(all_chains=False, provider="open-prices")


def test_bootstrap_creates_real_retailer_and_is_idempotent(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hermetic slug not present in ambient data, so creation is deterministic.
    monkeypatch.setitem(boot.AUTHORIZED_CHAINS, TEST_SLUG, "Bootstrap Test Chain")
    before = _counts(db_session)

    created = bootstrap(db_session, [TEST_SLUG])
    assert created == [TEST_SLUG]
    retailer = db_session.execute(
        select(Retailer).where(Retailer.slug == TEST_SLUG)
    ).scalar_one()
    assert retailer.name == "Bootstrap Test Chain"
    assert retailer.is_synthetic is False
    assert retailer.country == "ES"

    # Second run creates nothing; no product/price rows were ever touched.
    assert bootstrap(db_session, [TEST_SLUG]) == []
    assert _counts(db_session) == before


def test_bootstrap_refuses_unauthorized_chain(db_session: Session) -> None:
    with pytest.raises(ValueError, match="not authorized"):
        bootstrap(db_session, ["nestle"])
