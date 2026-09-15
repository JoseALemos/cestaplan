"""Geolocalización para el comparador multi-cadena (Fase 2).

Geocodifica el domicilio del hogar (Nominatim/OSM) y localiza la tienda más cercana de
cada cadena (Overpass/OSM) para estimar el coste de desplazamiento (€/km ida+vuelta) que
se descuenta del ahorro por comprar en un súper u otro. Todo con datos abiertos de
OpenStreetMap: el domicilio no sale a ningún tercero de pago y no se hace scraping de los
retailers. Ver docs de la feature y ``services/plan_comparison.py`` (Fase 1, precio puro).
"""

from __future__ import annotations
