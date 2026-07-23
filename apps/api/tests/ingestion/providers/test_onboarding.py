"""Retailer onboarding matrix (spec §1-§3) — offline.

Verifies the matrix declares the seven chains with the right scope, that config_status blocks
honestly per missing credential/base URL, and that upsert_activation records the matrix while
keeping rights under review and production unapproved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    PriceScope,
    SellUnit,
)
from cestaplan_api.ingestion.providers.onboarding import (
    RETAILER_MATRIX,
    config_status,
    get_entry,
    measure_coverage,
    upsert_activation,
)
from cestaplan_api.models import ProviderActivation

_NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _product(*, qty: Decimal | None, unit: ContentUnit | None) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="p",
        retailer_slug="r",
        external_product_id="x",
        product_name="Producto",
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal("1.00"),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        observed_at=_NOW,
        availability=Availability.IN_STOCK,
        net_content_quantity=qty,
        net_content_unit=unit,
    )


def test_sample_capture_is_never_costable() -> None:
    # A source without full-catalogue support -> sample_only regardless of package coverage.
    products = [_product(qty=Decimal("500"), unit=ContentUnit.G) for _ in range(10)]
    cov = measure_coverage(
        products,
        captured=10,
        limit=10,
        supports_full_catalog=False,
        supports_store_scope=False,
    )
    assert cov.observed_catalog_scope == "sample_only"
    assert cov.costing_eligibility == "insufficient"
    assert cov.geographic_scope_coverage == Decimal("0.0000")


def test_missing_package_content_blocks_costing() -> None:
    # Even a fully-enumerated catalogue is not costable without net-content coverage.
    products = [_product(qty=None, unit=None) for _ in range(3)]
    cov = measure_coverage(
        products,
        captured=3,
        limit=10,
        supports_full_catalog=True,
        supports_store_scope=False,
    )
    assert cov.observed_catalog_scope == "full"  # exhausted below the limit
    assert cov.price_coverage == Decimal("1.0000")
    assert cov.package_quantity_coverage == Decimal("0.0000")
    assert cov.costing_eligibility == "insufficient"


def test_full_catalog_with_package_content_is_costable() -> None:
    products = [_product(qty=Decimal("400"), unit=ContentUnit.G) for _ in range(3)]
    cov = measure_coverage(
        products,
        captured=3,
        limit=10,
        supports_full_catalog=True,
        supports_store_scope=True,
    )
    assert cov.observed_catalog_scope == "full"
    assert cov.package_unit_coverage == Decimal("1.0000")
    assert cov.geographic_scope_coverage == Decimal("1.0000")
    assert cov.costing_eligibility == "sufficient"


def test_empty_capture_is_unknown() -> None:
    cov = measure_coverage(
        [], captured=0, limit=10, supports_full_catalog=True, supports_store_scope=True
    )
    assert cov.observed_catalog_scope == "unknown"
    assert cov.costing_eligibility == "unknown"


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "parse_bot_api_key": "",
        "parse_bot_dia_base_url": "",
        "apify_api_token": "",
    }
    base.update(over)
    return Settings(**base)


def test_matrix_declares_seven_chains_plus_sources() -> None:
    codes = {e.provider_code for e in RETAILER_MATRIX}
    for chain in (
        "parsebot-dia",
        "parsebot-alcampo",
        "apify-mercadona",
        "parsebot-carrefour",
        "parsebot-lidl",
        "parsebot-aldi",
        "parsebot-deza",
    ):
        assert chain in codes
    assert "open-prices" in codes and "demo" in codes
    # partial sources are declared partial (never full)
    for chain in ("parsebot-lidl", "parsebot-aldi", "parsebot-deza"):
        entry = get_entry(chain)
        assert entry is not None
        assert entry.intended_catalog_scope == "partial"
    deza = get_entry("parsebot-deza")
    assert deza is not None and deza.authorized_feed_required is True


def test_config_status_blocks_honestly() -> None:
    dia = get_entry("parsebot-dia")
    assert dia is not None
    # no key -> missing credentials
    assert config_status(dia, _settings()).blocked_reason == "blocked_by_missing_credentials"
    # key but no base URL -> missing base URL
    s = _settings(parse_bot_api_key="k")
    assert config_status(dia, s).blocked_reason == "blocked_by_missing_base_url"
    # key + base URL -> configured
    s2 = _settings(parse_bot_api_key="k", parse_bot_dia_base_url="https://x")
    assert config_status(dia, s2).configured is True
    # apify without token
    merc = get_entry("apify-mercadona")
    assert merc is not None
    merc_status = config_status(merc, _settings())
    assert merc_status.blocked_reason == "blocked_by_missing_credentials"
    # open-prices / demo need no credentials
    op, demo = get_entry("open-prices"), get_entry("demo")
    assert op is not None and demo is not None
    assert config_status(op, _settings()).configured is True
    assert config_status(demo, _settings()).configured is True


def test_upsert_activation_records_matrix_without_activating_production(
    db_session: Session,
) -> None:
    entry = get_entry("parsebot-lidl")
    assert entry is not None
    row = upsert_activation(
        db_session, entry, now=_NOW, transport_status="down", mapper_status="blocked"
    )
    assert row.intended_role == "partial_offers"
    assert row.intended_catalog_scope == "partial"
    assert row.activation_state == "disabled"
    assert row.expected_capabilities == ["promotions"]
    assert row.data_rights_status == "under_review"  # never auto-cleared
    assert row.production_approved_at is None and row.production_approved_by is None
    # No capture -> observed coverage unknown, never costable, never production-eligible.
    assert row.observed_catalog_scope == "unknown"
    assert row.costing_eligibility == "unknown"
    assert row.production_eligibility is False

    # idempotent update
    upsert_activation(db_session, entry, now=_NOW, transport_status="down", mapper_status="blocked")
    count = db_session.scalar(
        select(ProviderActivation.id).where(ProviderActivation.provider_code == "parsebot-lidl")
    )
    assert count is not None
