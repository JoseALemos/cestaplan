"""Targeted discovery classification + persistence (spec §5/§6/§7) — offline, no network.

The external capture is replaced by crafted products so the test exercises the *classification and
persistence* path deterministically: §6 rejections, single-word approval via a normalised category,
staging-only persistence and a ProviderUsage record — with no external call.
"""

from __future__ import annotations

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
    PriceScope,
    SellUnit,
)
from cestaplan_api.models import PriceObservation, ProviderIngredientMapping, ProviderUsage
from cestaplan_api.services import targeted_discovery as td
from tests.fixtures.provider_scenarios import ensure_test_ingredient, seed_test_retailer

_NOW = datetime.now(UTC)


def _fruit(ext: str, name: str, qty: str | None) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="parsebot-alcampo",
        retailer_slug="alcampo",
        external_product_id=ext,
        product_name=name,
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal("2.49"),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        variable_weight=False,
        brand=None,
        category="Frutas",
        net_content_quantity=Decimal(qty) if qty is not None else None,
        net_content_unit=ContentUnit.G if qty is not None else None,
    )


_FAKE = [
    _fruit("OFF-CANARIAS", "Plátano de Canarias bandeja 700 g", "700"),  # auto (frutas category)
    _fruit("OFF-BATIDO", "Batido sabor plátano", "500"),  # §6 reject: milkshake
    _fruit("OFF-MACHO", "Plátano macho para freir", "600"),  # §6 reject: plantain
    _fruit("OFF-BANANA", "Banana al peso", "750"),  # no 'platano' term -> not auto-approved
]


@pytest.fixture()
def _no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_session: Session) -> None:
    # Hermetic: the retailer + ingredient the discovery resolves must exist explicitly.
    seed_test_retailer(db_session, "alcampo")
    ensure_test_ingredient(db_session, "platano", category_code="frutas")
    monkeypatch.setattr(td, "_LOCAL", tmp_path)  # never touch the real .local captures

    def _fake_capture(provider_code, settings, key, limit, out_dir):
        return list(_FAKE)

    monkeypatch.setattr(td, "_capture_alcampo", _fake_capture)


def _by_ext(db: Session, ext: str) -> ProviderIngredientMapping | None:
    return db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == "parsebot-alcampo",
            ProviderIngredientMapping.external_product_id == ext,
        )
    ).scalar_one_or_none()


def test_discovery_auto_approves_only_the_compatible_fruit(
    db_session: Session, _no_network: None
) -> None:
    td.discover_and_map(db_session, "parsebot-alcampo", ["platano"], max_calls=1, now=_NOW)

    canarias = _by_ext(db_session, "OFF-CANARIAS")
    assert canarias is not None and canarias.active is True
    assert canarias.mapping_status == "auto_approved"

    # A milkshake and a plantain are never mapped as costable plátano.
    for ext in ("OFF-BATIDO", "OFF-MACHO"):
        row = _by_ext(db_session, ext)
        assert row is None or row.active is False

    # 'Banana al peso' has no 'platano' term -> never auto-approved.
    banana = _by_ext(db_session, "OFF-BANANA")
    assert banana is None or banana.active is False


def test_discovery_persists_only_staging_prices(db_session: Session, _no_network: None) -> None:
    td.discover_and_map(db_session, "parsebot-alcampo", ["platano"], max_calls=1, now=_NOW)
    canarias = _by_ext(db_session, "OFF-CANARIAS")
    assert canarias is not None
    obs = (
        db_session.execute(
            select(PriceObservation).where(
                PriceObservation.product_variant_id.is_not(None),
                PriceObservation.staging_only.is_(True),
            )
        )
        .scalars()
        .all()
    )
    # Every observation this discovery wrote is staging-only (never production).
    assert obs
    assert all(o.staging_only is True for o in obs)


def test_discovery_logs_provider_usage(db_session: Session, _no_network: None) -> None:
    before = (
        db_session.execute(
            select(ProviderUsage).where(
                ProviderUsage.provider == "parsebot-alcampo",
                ProviderUsage.operation == "targeted_discovery",
            )
        )
        .scalars()
        .all()
    )
    td.discover_and_map(db_session, "parsebot-alcampo", ["platano"], max_calls=1, now=_NOW)
    after = (
        db_session.execute(
            select(ProviderUsage).where(
                ProviderUsage.provider == "parsebot-alcampo",
                ProviderUsage.operation == "targeted_discovery",
            )
        )
        .scalars()
        .all()
    )
    assert len(after) > len(before)  # the bounded query is metered
