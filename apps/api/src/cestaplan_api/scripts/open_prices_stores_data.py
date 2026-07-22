"""Real Spanish store locations for the Open Prices seed (Task 2).

Each chain maps to a list of **real** OpenStreetMap store locations that Open Prices has
observed prices for (researched from the live Open Prices ``/locations`` + ``/prices`` API).
These seed real ``Retailer`` (``is_synthetic=False``, ``adapter_key='open_prices'``) and real
``Store`` rows; the stores start with NO prices until synced (real, not fabricated).

Deza is deliberately absent: Open Prices has no data for it, so it is not seeded.
``price_count`` is the count observed at research time (informative only; the sync pulls the
live figure). Coordinates/city/postcode come straight from OSM via Open Prices.
"""

from __future__ import annotations

import re

#: {ChainName: (retailer_slug, [store dicts])}. Retailer display name is the ChainName.
OPEN_PRICES_CHAINS: dict[str, str] = {
    "Mercadona": "mercadona",
    "Aldi": "aldi",
    "Lidl": "lidl",
    "Carrefour": "carrefour",
    "Dia": "dia",
    "Alcampo": "alcampo",
}

#: Whole-word brand/name keywords per chain, used by the live ``--discover`` mode to map an
#: Open Prices OSM location to one of the 6 chains. Word-boundary matching keeps ``\bdia\b``
#: from matching "media"/"diagonal" and never matches Deza (deliberately absent → not seeded).
_CHAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Mercadona": ("mercadona",),
    "Aldi": ("aldi",),
    "Lidl": ("lidl",),
    "Carrefour": ("carrefour",),
    "Dia": ("dia",),
    "Alcampo": ("alcampo",),
}

_CHAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    chain: re.compile(
        r"\b(?:" + "|".join(re.escape(kw) for kw in kws) + r")\b", re.IGNORECASE
    )
    for chain, kws in _CHAIN_KEYWORDS.items()
}


def match_chain(brand: str | None, name: str | None) -> str | None:
    """Return the ChainName an OSM location's brand/name belongs to, or ``None``.

    Matches the chain keywords as whole words against ``"{brand} {name}"``; the first of the
    6 supported chains that matches wins. Deza (and everything else) yields ``None``.
    """
    haystack = f"{brand or ''} {name or ''}"
    for chain, pattern in _CHAIN_PATTERNS.items():
        if pattern.search(haystack):
            return chain
    return None

#: {ChainName: [ {osm_id, osm_type, name, city, pc, lat, lon, price_count} ]}
OPEN_PRICES_STORES: dict[str, list[dict[str, object]]] = {
    "Carrefour": [
        {"osm_id": 13283342695, "osm_type": "NODE", "name": "Carrefour Express",
         "city": "Barcelona", "pc": "08014", "lat": 41.3754791, "lon": 2.1370704,
         "price_count": 23},
        {"osm_id": 9806545750, "osm_type": "NODE", "name": "Carrefour Express",
         "city": "Granada", "pc": "18001", "lat": 37.1739039, "lon": -3.6007125,
         "price_count": 16},
    ],
    "Alcampo": [
        {"osm_id": 7091021880, "osm_type": "NODE", "name": "Alcampo",
         "city": "Barcelona", "pc": "08002", "lat": 41.3784, "lon": 2.17852,
         "price_count": 32},
        {"osm_id": 1214937211, "osm_type": "WAY", "name": "Alcampo",
         "city": "l'Hospitalet de Llobregat", "pc": "08904", "lat": 41.374194,
         "lon": 2.1179765, "price_count": 20},
    ],
    "Dia": [
        {"osm_id": 9238446768, "osm_type": "NODE", "name": "Dia & go",
         "city": "Barcelona", "pc": "08010", "lat": 41.3909216, "lon": 2.1771049,
         "price_count": 7},
        {"osm_id": 3859922461, "osm_type": "NODE", "name": "DIA&GO",
         "city": "Madrid", "pc": "28016", "lat": 40.4567576, "lon": -3.6758414,
         "price_count": 5},
    ],
    "Mercadona": [
        {"osm_id": 128567802, "osm_type": "WAY", "name": "Mercadona",
         "city": "León", "pc": "24005", "lat": 42.583533, "lon": -5.5562856,
         "price_count": 17},
        {"osm_id": 3637813086, "osm_type": "NODE", "name": "Mercadona",
         "city": "La Mancha", "pc": "38430", "lat": 28.3752781, "lon": -16.7054278,
         "price_count": 15},
    ],
    "Lidl": [
        {"osm_id": 677280352, "osm_type": "WAY", "name": "Lidl",
         "city": "Sant Joan d'Alacant", "pc": "03550", "lat": 38.3918179,
         "lon": -0.4333982, "price_count": 42},
        {"osm_id": 1113418042, "osm_type": "WAY", "name": "Lidl",
         "city": "Olot", "pc": "17800", "lat": 42.1881958, "lon": 2.4697911,
         "price_count": 17},
    ],
    "Aldi": [
        {"osm_id": 868615657, "osm_type": "WAY", "name": "ALDI",
         "city": "Tres Cantos", "pc": "28760", "lat": 40.6145428, "lon": -3.7072136,
         "price_count": 3},
        {"osm_id": 915548279, "osm_type": "WAY", "name": "ALDI",
         "city": "Madrid", "pc": "28023", "lat": 40.4645971, "lon": -3.798414,
         "price_count": 2},
    ],
}
