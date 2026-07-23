"""Retailer onboarding matrix (spec §1-§3) — offline.

Verifies the matrix declares the seven chains with the right scope, that config_status blocks
honestly per missing credential/base URL, and that upsert_activation records the matrix while
keeping rights under review and production unapproved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.onboarding import (
    RETAILER_MATRIX,
    config_status,
    get_entry,
    upsert_activation,
)
from cestaplan_api.models import ProviderActivation

_NOW = datetime(2026, 7, 23, tzinfo=UTC)


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
        assert get_entry(chain).catalog_scope == "partial"  # type: ignore[union-attr]
    assert get_entry("parsebot-deza").authorized_feed_required is True  # type: ignore[union-attr]


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
    assert row.catalog_scope == "partial"
    assert row.activation_state == "disabled"
    assert row.expected_capabilities == ["promotions"]
    assert row.data_rights_status == "under_review"  # never auto-cleared
    assert row.production_approved_at is None and row.production_approved_by is None

    # idempotent update
    upsert_activation(db_session, entry, now=_NOW, transport_status="down", mapper_status="blocked")
    count = db_session.scalar(
        select(ProviderActivation.id).where(ProviderActivation.provider_code == "parsebot-lidl")
    )
    assert count is not None
