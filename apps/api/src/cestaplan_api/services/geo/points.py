"""Punto geográfico + distancia haversine (línea recta, sin coste de carretera).

Todo en :class:`~decimal.Decimal` en la frontera pública (nunca ``float``, ver
docs/DATA_MODEL.md §1): la distancia se calcula internamente en ``float`` (aritmética
esférica estándar) porque la precisión al milímetro es irrelevante para un coste de
desplazamiento, pero el valor devuelto se convierte siempre a ``Decimal``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

# Radio medio de la Tierra (km), convención IUGG usada por la fórmula haversine estándar.
_EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class GeoPoint:
    """Coordenada (lat, lon) en grados decimales."""

    lat: Decimal
    lon: Decimal


def haversine_km(a: GeoPoint, b: GeoPoint) -> Decimal:
    """Distancia en línea recta entre ``a`` y ``b`` (km, redondeada a mm, siempre >= 0)."""
    lat1, lon1 = math.radians(float(a.lat)), math.radians(float(a.lon))
    lat2, lon2 = math.radians(float(b.lat)), math.radians(float(b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance = 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))
    return Decimal(str(round(distance, 3)))


__all__ = ["GeoPoint", "haversine_km"]
