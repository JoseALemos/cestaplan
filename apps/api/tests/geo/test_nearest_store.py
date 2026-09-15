"""nearest_store: brand filtering + nearest pick, against a monkeypatched Overpass response.

No network: ``httpx.post`` inside ``services.geo.stores`` is replaced with a fixed payload.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from cestaplan_api.config import Settings
from cestaplan_api.services.geo import stores as stores_module
from cestaplan_api.services.geo.points import GeoPoint

_ORIGIN = GeoPoint(lat=Decimal("40.4168"), lon=Decimal("-3.7038"))

# Node coordinates chosen so the "far" Mercadona is clearly further than the "near" one.
_OVERPASS_PAYLOAD = {
    "elements": [
        {
            "type": "node",
            "lat": 40.4200,
            "lon": -3.7000,
            "tags": {"shop": "supermarket", "brand": "Mercadona", "name": "Mercadona"},
        },
        {
            "type": "way",
            "center": {"lat": 40.5000, "lon": -3.6000},
            "tags": {"shop": "supermarket", "brand": "Mercadona", "name": "Mercadona"},
        },
        {
            "type": "node",
            "lat": 40.4180,
            "lon": -3.7020,
            "tags": {"shop": "supermarket", "brand": "Carrefour", "name": "Carrefour Express"},
        },
    ]
}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_post(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    def _fake_post(*args: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(payload)

    monkeypatch.setattr(stores_module.httpx, "post", _fake_post)


def test_nearest_mercadona_chosen_and_carrefour_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, _OVERPASS_PAYLOAD)
    settings = Settings()
    hit = stores_module.nearest_store(_ORIGIN, ["Mercadona"], settings)
    assert hit is not None
    assert hit.name == "Mercadona"
    # The near node (40.4200, -3.7000) must win over the far way (40.5000, -3.6000).
    assert hit.point.lat == Decimal("40.42")


def test_empty_elements_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, {"elements": []})
    settings = Settings()
    assert stores_module.nearest_store(_ORIGIN, ["Mercadona"], settings) is None


def test_no_matching_brand_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, _OVERPASS_PAYLOAD)
    settings = Settings()
    assert stores_module.nearest_store(_ORIGIN, ["Lidl"], settings) is None
