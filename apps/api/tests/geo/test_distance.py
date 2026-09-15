"""Haversine distance: identity, a known city pair, and the Decimal return type."""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.services.geo.points import GeoPoint, haversine_km

_MADRID = GeoPoint(lat=Decimal("40.4168"), lon=Decimal("-3.7038"))
_BARCELONA = GeoPoint(lat=Decimal("41.3874"), lon=Decimal("2.1686"))


def test_identical_points_have_zero_distance() -> None:
    assert haversine_km(_MADRID, _MADRID) == Decimal("0.000")


def test_madrid_barcelona_distance_is_approximately_505km() -> None:
    distance = haversine_km(_MADRID, _BARCELONA)
    assert isinstance(distance, Decimal)
    assert abs(distance - Decimal("505")) <= Decimal("5")
