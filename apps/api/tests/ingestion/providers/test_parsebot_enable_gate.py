"""Per-chain enable gate: a Parse.bot chain reaches the network ONLY when globally + per-chain
enabled AND key + base URL are set. A present base URL with the flag off never opens a network
path, and enabling one chain never enables another."""

from __future__ import annotations

import pytest

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.contracts import ProductQuery
from cestaplan_api.ingestion.providers.exceptions import NotSupportedError
from cestaplan_api.ingestion.providers.parsebot import plans
from cestaplan_api.ingestion.providers.parsebot.chains import ParseBotAlcampoProvider

_URL = "https://api.parse.bot/alcampo-scraper"
_CARREFOUR_URL = "https://api.parse.bot/carrefour-scraper"


def _settings(**overrides: object) -> Settings:
    """Fully-specified config (so the ambient .env never leaks in) with Alcampo enabled + configured
    and Carrefour disabled by default."""
    base: dict[str, object] = {
        "parse_bot_enabled": True,
        "parse_bot_api_key": "test-key",
        "parse_bot_alcampo_enabled": True,
        "parse_bot_alcampo_base_url": _URL,
        "parse_bot_carrefour_enabled": False,
        "parse_bot_carrefour_base_url": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_is_configured_true_when_everything_is_set() -> None:
    assert plans.is_configured("parsebot-alcampo", _settings()) is True


def test_per_chain_flag_off_blocks_even_with_url_present() -> None:
    s = _settings(parse_bot_alcampo_enabled=False)
    assert plans.is_configured("parsebot-alcampo", s) is False


def test_global_flag_off_blocks_all_chains() -> None:
    assert plans.is_configured("parsebot-alcampo", _settings(parse_bot_enabled=False)) is False


def test_url_absent_blocks_even_when_enabled() -> None:
    s = _settings(parse_bot_alcampo_base_url="")
    assert plans.is_configured("parsebot-alcampo", s) is False


def test_key_absent_blocks() -> None:
    assert plans.is_configured("parsebot-alcampo", _settings(parse_bot_api_key="")) is False


def test_capture_records_disabled_raises_before_any_network() -> None:
    # A disabled chain never builds a client or calls the network — it fails fast.
    s = _settings(parse_bot_alcampo_enabled=False)
    with pytest.raises(RuntimeError, match="deshabilitado"):
        plans.capture_records("parsebot-alcampo", s, limit=5)


def test_provider_iterate_blocked_when_disabled() -> None:
    provider = ParseBotAlcampoProvider(settings=_settings(parse_bot_alcampo_enabled=False))
    assert provider._configured() is False
    assert provider.health_check().ok is False
    with pytest.raises(NotSupportedError):
        list(provider.iterate_products(ProductQuery()))


def test_enabling_one_chain_does_not_enable_another() -> None:
    # Alcampo fully enabled; Carrefour has a base URL present but its flag stays off -> blocked.
    s = _settings(parse_bot_carrefour_base_url=_CARREFOUR_URL)
    assert plans.is_configured("parsebot-alcampo", s) is True
    assert plans.is_configured("parsebot-carrefour", s) is False
