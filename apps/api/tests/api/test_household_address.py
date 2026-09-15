"""PATCH /households/{id}/address: sets the address and geocodes it (no network)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from cestaplan_api.services.geo import household_geo
from cestaplan_api.services.geo.points import GeoPoint

from .conftest import csrf, login, register


def _email() -> str:
    return f"addr-{uuid.uuid4().hex[:12]}@example.com"


def _create_household(client, token: str) -> dict:
    resp = client.post("/api/v1/households", json={"name": "Casa"}, headers=csrf(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_address_geocoded_ok(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        household_geo,
        "geocode",
        lambda *a, **k: GeoPoint(lat=Decimal("37.8882"), lon=Decimal("-4.7794")),
    )

    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)

    resp = client.patch(
        f"/api/v1/households/{hh['id']}/address",
        json={"address_text": "Calle Falsa 123", "postal_code": "14006", "city": "Córdoba"},
        headers=csrf(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["address_text"] == "Calle Falsa 123"
    assert body["postal_code"] == "14006"
    assert body["city"] == "Córdoba"
    assert body["geocode_status"] == "ok"
    assert Decimal(body["latitude"]) == Decimal("37.8882")
    assert Decimal(body["longitude"]) == Decimal("-4.7794")


def test_address_not_found(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(household_geo, "geocode", lambda *a, **k: None)

    email = _email()
    register(client, email)
    token = login(client, email)
    hh = _create_household(client, token)

    resp = client.patch(
        f"/api/v1/households/{hh['id']}/address",
        json={"address_text": "Dirección inexistente xyz"},
        headers=csrf(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["geocode_status"] == "not_found"
    assert body["latitude"] is None
    assert body["longitude"] is None
