"""Adapter registry tests: every adapter is listed with the correct enabled/disabled state."""

from __future__ import annotations

import pytest

from cestaplan_api.adapters import get_adapter, list_adapters
from cestaplan_api.adapters.base import AdapterStatus, NotSupportedError
from cestaplan_api.adapters.registry import AdapterListing


def _by_key() -> dict[str, AdapterListing]:
    return {listing.adapter_key: listing for listing in list_adapters()}


def test_all_expected_adapters_registered() -> None:
    keys = set(_by_key())
    assert {
        "demo",
        "csv",
        "json",
        "manual",
        "mercadona_community",
        "aldi",
        "lidl",
        "carrefour",
        "dia",
        "alcampo",
        "deza",
    } <= keys


def test_active_adapters_enabled() -> None:
    listings = _by_key()
    for key in ("demo", "csv", "json", "manual"):
        assert listings[key].enabled is True
        assert listings[key].status is AdapterStatus.ACTIVE


def test_community_adapter_disabled_by_default() -> None:
    merca = _by_key()["mercadona_community"]
    assert merca.enabled is False
    assert merca.is_community is True
    assert merca.status is AdapterStatus.EXPERIMENTAL
    assert merca.source_type == "community_connector"


def test_chain_skeletons_disabled() -> None:
    listings = _by_key()
    for key in ("aldi", "lidl", "carrefour", "dia", "alcampo", "deza"):
        assert listings[key].enabled is False
        assert listings[key].status is AdapterStatus.SKELETON


def test_skeleton_catalog_raises_not_implemented() -> None:
    adapter = get_adapter("aldi")
    assert adapter is not None
    from cestaplan_api.adapters.base import StoreSelector

    with pytest.raises(NotImplementedError):
        adapter.get_store_catalog(StoreSelector(retailer_slug="aldi"))


def test_unsupported_read_method_is_explicit() -> None:
    adapter = get_adapter("csv")
    assert adapter is not None
    from cestaplan_api.adapters.base import StoreSelector

    with pytest.raises(NotSupportedError):
        adapter.get_price("x", StoreSelector(retailer_slug="acme"))
