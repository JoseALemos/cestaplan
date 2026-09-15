"""Orquestación geo del hogar: geocodificar el domicilio y cachear la tienda más cercana
de cada cadena, para alimentar el coste de desplazamiento del comparador (Fase 2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Household, HouseholdChainStore, Retailer
from cestaplan_api.services.geo.geocoding import geocode
from cestaplan_api.services.geo.points import GeoPoint
from cestaplan_api.services.geo.stores import brands_for_retailer, nearest_store
from cestaplan_api.services.geo.travel import travel_cost_eur

if TYPE_CHECKING:
    from cestaplan_api.config import Settings


def geocode_household(db: Session, household: Household, settings: Settings) -> None:
    """Geocodifica el domicilio declarado del hogar y actualiza sus columnas de coordenadas.

    ``geocode_status`` queda en ``"disabled"`` si geo está deshabilitado, ``"not_found"`` si
    Nominatim no resuelve la dirección, u ``"ok"`` si se obtuvo un punto.
    """
    if not settings.geo_enabled:
        household.latitude = None
        household.longitude = None
        household.geocoded_at = datetime.now(UTC)
        household.geocode_status = "disabled"
        db.flush()
        return

    point = geocode(household.address_text, household.postal_code, household.city, settings)
    household.geocoded_at = datetime.now(UTC)
    if point is None:
        household.latitude = None
        household.longitude = None
        household.geocode_status = "not_found"
    else:
        household.latitude = point.lat
        household.longitude = point.lon
        household.geocode_status = "ok"
    db.flush()


def _is_fresh(row: HouseholdChainStore, *, ttl_days: int, now: datetime) -> bool:
    return row.computed_at >= now - timedelta(days=ttl_days)


def refresh_nearest_stores(
    db: Session, household: Household, retailers: list[Retailer], settings: Settings
) -> None:
    """Actualiza (o deja intacto si sigue fresco) el caché de tienda más cercana por cadena.

    No-op si el hogar no tiene coordenadas o geo está deshabilitado (nunca inventa una
    distancia sin domicilio geocodificado).
    """
    if not settings.geo_enabled or household.latitude is None or household.longitude is None:
        return

    point = GeoPoint(lat=household.latitude, lon=household.longitude)
    now = datetime.now(UTC)

    existing = {
        row.retailer_id: row
        for row in db.execute(
            select(HouseholdChainStore).where(
                HouseholdChainStore.household_id == household.id
            )
        )
        .scalars()
        .all()
    }

    for retailer in retailers:
        row = existing.get(retailer.id)
        if row is not None and _is_fresh(row, ttl_days=settings.geo_cache_ttl_days, now=now):
            continue

        brands = brands_for_retailer(retailer.slug, settings)
        hit = nearest_store(point, brands, settings)

        if row is None:
            row = HouseholdChainStore(
                household_id=household.id,
                retailer_id=retailer.id,
                computed_at=now,
            )
            db.add(row)

        if hit is None:
            row.found = False
            row.store_name = None
            row.latitude = None
            row.longitude = None
            row.distance_km = None
        else:
            row.found = True
            row.store_name = hit.name
            row.latitude = hit.point.lat
            row.longitude = hit.point.lon
            row.distance_km = hit.distance_km
        row.computed_at = now
        row.source = "overpass"

    db.flush()


def travel_by_retailer(
    db: Session, household: Household, retailers: list[Retailer], settings: Settings
) -> dict[int, dict[str, Any]]:
    """Distancia/coste de desplazamiento por cadena, ``{}`` si el hogar no tiene domicilio.

    Refresca el caché primero (:func:`refresh_nearest_stores`) y nunca inventa un dato: una
    cadena sin tienda encontrada aparece con ``found=False`` y valores ``None``.
    """
    if household.latitude is None or household.longitude is None:
        return {}

    refresh_nearest_stores(db, household, retailers, settings)

    rows = {
        row.retailer_id: row
        for row in db.execute(
            select(HouseholdChainStore).where(
                HouseholdChainStore.household_id == household.id
            )
        )
        .scalars()
        .all()
    }

    result: dict[int, dict[str, Any]] = {}
    for retailer in retailers:
        row = rows.get(retailer.id)
        if row is None or not row.found or row.distance_km is None:
            result[retailer.id] = {
                "distance_km": None,
                "travel_cost": None,
                "nearest_store": None,
                "found": False,
            }
            continue
        distance_km: Decimal = row.distance_km
        result[retailer.id] = {
            "distance_km": distance_km,
            "travel_cost": travel_cost_eur(distance_km, settings),
            "nearest_store": {
                "name": row.store_name,
                "latitude": row.latitude,
                "longitude": row.longitude,
            },
            "found": True,
        }
    return result


__all__ = ["geocode_household", "refresh_nearest_stores", "travel_by_retailer"]
