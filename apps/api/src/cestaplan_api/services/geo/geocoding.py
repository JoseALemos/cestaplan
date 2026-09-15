"""Geocodificación del domicilio del hogar vía Nominatim (OpenStreetMap).

Convierte una dirección de texto en coordenadas. Dato abierto, sin coste, sin clave de API.
NUNCA lanza al llamador: un fallo de red/timeout/respuesta vacía se registra como warning y
se devuelve ``None`` (una coordenada nunca se inventa — ver ``plan_comparison.py``).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import httpx

from cestaplan_api.services.geo.points import GeoPoint

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

logger = logging.getLogger(__name__)


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"User-Agent": settings.scraping_user_agent}
    if settings.scraping_contact_email:
        headers["From"] = settings.scraping_contact_email
    return headers


def geocode(
    address_text: str | None,
    postal_code: str | None,
    city: str | None,
    settings: Settings,
) -> GeoPoint | None:
    """Geocodifica una dirección (Nominatim ``/search``); ``None`` si no se resuelve.

    Respeta ``settings.geo_enabled`` (``False`` -> ``None`` sin llamar a la red).
    """
    if not settings.geo_enabled:
        return None
    candidates = (address_text, postal_code, city, settings.geo_country)
    parts = [p.strip() for p in candidates if p and p.strip()]
    query = ", ".join(parts)
    if not query:
        return None

    try:
        response = httpx.get(
            f"{settings.nominatim_base_url}/search",
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "es",
                "addressdetails": 0,
            },
            headers=_headers(settings),
            timeout=settings.geo_timeout_seconds,
        )
        response.raise_for_status()
        results = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim geocoding failed for %r: %s", query, exc)
        return None

    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    try:
        return GeoPoint(lat=Decimal(str(first["lat"])), lon=Decimal(str(first["lon"])))
    except (KeyError, InvalidOperation, TypeError) as exc:
        logger.warning("Nominatim returned an unparsable result for %r: %s", query, exc)
        return None


__all__ = ["geocode"]
