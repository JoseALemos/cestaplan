"""Discovery -> candidates for a SEARCH-capable Apify provider (apify-mercadona), offline.

The capture route is chosen by the provider's declared ``search`` capability (never a hardcoded
name): apify-mercadona is registered, declares search and is NOT a Parse.bot plan chain, so
discovery captures per ingredient through the generic ``iterate_products`` contract instead of the
Parse.bot ``plans.capture_records`` path. Here the Apify provider is mocked to return a couple of
mapped products per keyword, so the whole discover -> classify -> candidate flow runs with no
network. Every match is a REVIEW_ONLY candidate (never active/production).

Parse.bot's plan-capture route is unaffected (proven by the existing offline discovery tests).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    PriceScope,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderStatus,
    SellUnit,
)
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import PriceObservation, ProviderIngredientMapping
from cestaplan_api.services import targeted_discovery as td
from tests.fixtures.provider_scenarios import ensure_test_ingredient, seed_test_retailer

_NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
_KEYS = ["aceite_oliva", "leche", "huevo"]


def _product(
    ext: str, name: str, price: str, qty: str, unit: ContentUnit
) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="apify-mercadona",
        retailer_slug="mercadona",
        external_product_id=ext,
        product_name=name,
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal(price),
        currency="EUR",
        price_scope=PriceScope.POSTAL_CODE,
        postal_code="28001",
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        net_content_quantity=Decimal(qty),
        net_content_unit=unit,
    )


# Keyed by the search term discovery passes (``specs()[key].aliases[0]``): 2 mapped products each.
_L = ContentUnit.L
_U = ContentUnit.UNIT
_BY_QUERY: dict[str, list[ExternalCatalogProduct]] = {
    "aceite de oliva": [
        _product("MERC-ACE-1", "Aceite de oliva virgen extra Hacendado 1 L", "6.50", "1", _L),
        _product("MERC-ACE-2", "Aceite de oliva suave Hacendado 1 L", "5.90", "1", _L),
    ],
    "leche": [
        _product("MERC-LEC-1", "Leche entera Hacendado 1 L", "0.89", "1", _L),
        _product("MERC-LEC-2", "Leche semidesnatada Hacendado 1 L", "0.85", "1", _L),
    ],
    "huevo": [
        _product("MERC-HUE-1", "Huevos frescos L Hacendado docena", "2.10", "12", _U),
        _product("MERC-HUE-2", "Huevos camperos M media docena", "1.80", "6", _U),
    ],
}


class _FakeApifyProvider(PriceCatalogProvider):
    """A search-capable, registered provider standing in for the real Apify Mercadona actor."""

    provider_code = "apify-mercadona"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            store_scope=True, promotions=True, categories=True, search=True
        )

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code=self.provider_code,
            retailer_slug="mercadona",
            kind=ProviderKind.INDEPENDENT,
            status=ProviderStatus.ACTIVE_WHEN_CONFIGURED,
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="fake")

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        yield from _BY_QUERY.get(query.search or "", [])


@pytest.fixture()
def _apify_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_session: Session
) -> None:
    # Hermetic: the mercadona retailer + the discovered ingredients must exist explicitly.
    seed_test_retailer(db_session, "mercadona")
    for key in _KEYS:
        ensure_test_ingredient(db_session, key)
    monkeypatch.setattr(td, "_LOCAL", tmp_path)  # never touch the real .local captures
    # Route apify-mercadona to the fake provider so iterate_products runs offline.
    monkeypatch.setitem(registry._factories, "apify-mercadona", _FakeApifyProvider)


def _mappings(db: Session) -> list[ProviderIngredientMapping]:
    return list(
        db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.provider_code == "apify-mercadona"
            )
        ).scalars()
    )


def test_apify_discovery_creates_review_only_candidates(
    db_session: Session, _apify_discovery: None
) -> None:
    report = td.discover_and_map(
        db_session, "apify-mercadona", _KEYS, now=_NOW,
        approval_mode=td.ApprovalMode.REVIEW_ONLY,
    )

    # The capability-driven route captured per ingredient (iterate_products), not staged reuse.
    assert report.queries == len(_KEYS)
    assert report.products_seen == 6  # 2 mapped products per keyword

    rows = _mappings(db_session)
    assert rows, "expected candidates from the searched ingredients"
    # Every ingredient produced at least one candidate.
    keys_with_candidates = {r.canonical_ingredient_key for r in rows}
    assert set(_KEYS) <= keys_with_candidates
    # Everything is a REVIEW_ONLY candidate: never active, never reviewed, v2.
    for r in rows:
        assert r.mapping_status == "candidate"
        assert r.required_review is True
        assert r.active is False
        assert r.reviewed_at is None and r.reviewed_by is None
        assert r.mapping_version == "2.0.0"


def test_apify_discovery_persists_only_staging_prices(
    db_session: Session, _apify_discovery: None
) -> None:
    td.discover_and_map(
        db_session, "apify-mercadona", _KEYS, now=_NOW,
        approval_mode=td.ApprovalMode.REVIEW_ONLY,
    )
    obs = list(
        db_session.execute(
            select(PriceObservation).where(PriceObservation.product_variant_id.is_not(None))
        ).scalars()
    )
    assert obs
    assert all(o.staging_only is True for o in obs)  # never a productive price
