"""Coste de desplazamiento (€) a partir de una distancia en línea recta.

La distancia haversine (línea recta) se corrige con un factor de "rodeo" de carretera
(``travel_road_detour_factor``, la carretera real siempre es más larga que la línea recta)
y se cuenta IDA Y VUELTA (``x2``); el resultado se cuantiza a céntimos con ``ROUND_HALF_UP``
(dinero, nunca float — ver docs/DATA_MODEL.md §1).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

_CENTS = Decimal("0.01")
_ROUND_TRIP = Decimal("2")


def travel_cost_eur(distance_km: Decimal, settings: Settings) -> Decimal:
    """Coste de ida y vuelta (€) para ``distance_km`` de línea recta, con los parámetros de
    ``settings`` (factor de rodeo + tarifa €/km)."""
    raw = (
        distance_km
        * settings.travel_road_detour_factor
        * _ROUND_TRIP
        * settings.travel_cost_eur_per_km
    )
    return raw.quantize(_CENTS, rounding=ROUND_HALF_UP)


__all__ = ["travel_cost_eur"]
