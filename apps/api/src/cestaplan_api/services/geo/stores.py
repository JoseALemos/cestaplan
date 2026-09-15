"""Tienda física más cercana de una cadena, vía Overpass (OpenStreetMap).

Consulta Overpass QL por supermercados (``shop=supermarket``) en un radio alrededor del
domicilio del hogar y filtra por marca/nombre/operador. Dato abierto, sin scraping de los
retailers. NUNCA lanza al llamador: cualquier fallo (red, timeout, respuesta vacía, marca no
encontrada) se registra como warning y devuelve ``None`` — una tienda/distancia nunca se
inventa (ver ``plan_comparison.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx

from cestaplan_api.services.geo.points import GeoPoint, haversine_km

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

logger = logging.getLogger(__name__)

# Marcas OSM por defecto para las cadenas conocidas del comparador (fusionadas con
# ``settings.retailer_osm_brand_map`` — ver config.py). Ampliar aquí a medida que se
# incorporen más cadenas visibles.
DEFAULT_OSM_BRANDS: dict[str, list[str]] = {
    "mercadona": ["Mercadona"],
    "dia": ["Dia", "DIA", "La Plaza de DIA"],
}


@dataclass(frozen=True)
class StoreHit:
    """Tienda encontrada más cercana al punto de búsqueda."""

    name: str | None
    point: GeoPoint
    distance_km: Decimal


def brands_for_retailer(slug: str, settings: Settings) -> list[str]:
    """Marcas OSM a buscar para la cadena ``slug`` (override de settings sobre el default)."""
    return settings.retailer_osm_brand_map.get(slug, DEFAULT_OSM_BRANDS.get(slug, []))


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"User-Agent": settings.scraping_user_agent}
    if settings.scraping_contact_email:
        headers["From"] = settings.scraping_contact_email
    return headers


def _overpass_query(point: GeoPoint, settings: Settings) -> str:
    radius = settings.geo_search_radius_m
    lat, lon = point.lat, point.lon
    timeout = int(settings.geo_timeout_seconds)
    return (
        f"[out:json][timeout:{timeout}];"
        f'(node["shop"="supermarket"](around:{radius},{lat},{lon});'
        f'way["shop"="supermarket"](around:{radius},{lat},{lon}););'
        "out center tags;"
    )


def _element_point(element: dict[str, Any]) -> GeoPoint | None:
    """Punto de un elemento Overpass: ``lat``/``lon`` directos (node) o ``center`` (way)."""
    try:
        if "lat" in element and "lon" in element:
            return GeoPoint(lat=Decimal(str(element["lat"])), lon=Decimal(str(element["lon"])))
        center = element.get("center")
        if isinstance(center, dict) and "lat" in center and "lon" in center:
            return GeoPoint(lat=Decimal(str(center["lat"])), lon=Decimal(str(center["lon"])))
    except (InvalidOperation, TypeError):
        return None
    return None


def _matches_brand(element: dict[str, Any], brands: list[str]) -> bool:
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return False
    candidates = [
        str(tags.get(key, "")) for key in ("brand", "name", "operator") if tags.get(key)
    ]
    haystacks = [c.lower() for c in candidates if c]
    return any(brand.lower() in haystack for brand in brands for haystack in haystacks)


def nearest_store(point: GeoPoint, brands: list[str], settings: Settings) -> StoreHit | None:
    """Tienda más cercana a ``point`` cuya marca/nombre/operador contenga alguna de ``brands``.

    ``None`` si geo está deshabilitado, la petición falla, no hay elementos o ninguno coincide.
    """
    if not settings.geo_enabled or not brands:
        return None

    try:
        response = httpx.post(
            settings.overpass_base_url,
            data={"data": _overpass_query(point, settings)},
            headers=_headers(settings),
            timeout=settings.geo_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Overpass query failed: %s", exc)
        return None

    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return None

    hits: list[StoreHit] = []
    for element in elements:
        if not isinstance(element, dict) or not _matches_brand(element, brands):
            continue
        element_point = _element_point(element)
        if element_point is None:
            continue
        tags = element.get("tags") or {}
        name = tags.get("name") or tags.get("brand")
        hits.append(
            StoreHit(
                name=name,
                point=element_point,
                distance_km=haversine_km(point, element_point),
            )
        )
    if not hits:
        return None
    return min(hits, key=lambda h: h.distance_km)


__all__ = ["DEFAULT_OSM_BRANDS", "StoreHit", "brands_for_retailer", "nearest_store"]
