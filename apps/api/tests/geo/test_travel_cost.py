"""Travel cost: round-trip distance x road-detour factor x €/km rate, quantized to cents."""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.config import Settings
from cestaplan_api.services.geo.travel import travel_cost_eur


def test_travel_cost_with_default_settings() -> None:
    # 10 km * 1.3 (detour) * 2 (round trip) * 0.26 €/km = 6.76 €.
    settings = Settings()
    cost = travel_cost_eur(Decimal("10"), settings)
    assert cost == Decimal("6.76")


def test_travel_cost_with_overridden_rate() -> None:
    settings = Settings(
        travel_cost_eur_per_km=Decimal("0.50"), travel_road_detour_factor=Decimal("1.0")
    )
    # 10 km * 1.0 * 2 * 0.50 €/km = 10.00 €.
    cost = travel_cost_eur(Decimal("10"), settings)
    assert cost == Decimal("10.00")
