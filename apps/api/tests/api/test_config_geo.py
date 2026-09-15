"""Geo/travel settings (Fase 2): defaults + retailer_osm_brand_map merge semantics."""

from __future__ import annotations

import json
from decimal import Decimal

from cestaplan_api.config import Settings


def test_travel_defaults() -> None:
    settings = Settings()
    assert settings.geo_enabled is True
    assert settings.travel_cost_eur_per_km == Decimal("0.26")
    assert settings.travel_road_detour_factor == Decimal("1.3")


def test_retailer_osm_brand_map_defaults_without_override() -> None:
    settings = Settings()
    brand_map = settings.retailer_osm_brand_map
    assert brand_map["mercadona"] == ["Mercadona"]
    assert brand_map["dia"] == ["Dia", "DIA", "La Plaza de DIA"]


def test_retailer_osm_brand_map_override_merges_over_defaults() -> None:
    settings = Settings(
        retailer_osm_brands=json.dumps(
            {"mercadona": ["Mercadona", "Mercadona Online"], "carrefour": ["Carrefour"]}
        )
    )
    brand_map = settings.retailer_osm_brand_map
    # Overridden slug replaces its own list...
    assert brand_map["mercadona"] == ["Mercadona", "Mercadona Online"]
    # ...an untouched default slug survives...
    assert brand_map["dia"] == ["Dia", "DIA", "La Plaza de DIA"]
    # ...and a brand-new slug is added.
    assert brand_map["carrefour"] == ["Carrefour"]


def test_retailer_osm_brand_map_invalid_json_falls_back_to_defaults() -> None:
    settings = Settings(retailer_osm_brands="not json")
    assert settings.retailer_osm_brand_map["mercadona"] == ["Mercadona"]
