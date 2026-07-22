"""Live-discovery (``--discover``) tests — HTTPX mocked, NO network.

Covers: :meth:`OpenPricesAdapter.fetch_locations` paginating ``/locations`` and filtering to
ES client-side; the ``seed_open_prices_stores.discover`` upsert that seeds ONLY priced ES
locations of the 6 chains (never Deza, never non-ES, never ``price_count == 0``); the
whole-word chain matcher; and idempotency (re-running upserts by ``external_code``).
"""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OpenPricesAdapter
from cestaplan_api.models import Store
from cestaplan_api.scripts.open_prices_stores_data import match_chain
from cestaplan_api.scripts.seed_open_prices_stores import discover


def _loc(
    osm_id: int,
    *,
    brand: str | None,
    name: str,
    cc: str = "ES",
    price_count: int = 5,
    osm_type: str = "NODE",
) -> dict:
    return {
        "osm_id": osm_id,
        "osm_type": osm_type,
        "osm_name": name,
        "osm_brand": brand,
        "osm_address_country_code": cc,
        "osm_address_city": "Madrid",
        "osm_address_postcode": "28001",
        "osm_lat": 40.4,
        "osm_lon": -3.7,
        "price_count": price_count,
    }


_LOCATIONS = [
    _loc(1, brand="Mercadona", name="Mercadona"),
    _loc(2, brand="ALDI", name="Aldi Supermercados"),
    _loc(3, brand="Lidl", name="Lidl"),
    _loc(4, brand="Carrefour Express", name="Carrefour Express"),
    _loc(5, brand="DIA", name="Dia & go"),
    _loc(6, brand="Alcampo", name="Alcampo"),
    _loc(7, brand="Deza", name="Deza"),  # excluded: not one of the 6 chains
    _loc(8, brand="Carrefour", name="Carrefour", cc="FR"),  # excluded: not ES
    _loc(9, brand="Lidl", name="Lidl", price_count=0),  # excluded: no prices
    _loc(10, brand="Media Markt", name="Media Markt"),  # \bdia\b must NOT match "media"
]


def _discover_adapter(pages: list[list[dict]]) -> OpenPricesAdapter:
    """A mocked adapter whose ``/locations`` serves the given pages in order."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/locations" in str(request.url)
        page = int(request.url.params.get("page", "1"))
        idx = page - 1
        items = pages[idx] if 0 <= idx < len(pages) else []
        return httpx.Response(
            200, json={"items": items, "page": page, "pages": len(pages), "size": 100}
        )

    return OpenPricesAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------- #
# match_chain: whole-word, the 6 chains only, never Deza/false positives
# --------------------------------------------------------------------------- #
def test_match_chain_maps_the_six_and_excludes_deza() -> None:
    assert match_chain("Mercadona", "Mercadona") == "Mercadona"
    assert match_chain("Carrefour Express", "Carrefour Express") == "Carrefour"
    assert match_chain("DIA", "Dia & go") == "Dia"
    assert match_chain("Deza", "Deza") is None
    assert match_chain("Media Markt", "Media Markt") is None  # 'dia' inside 'media'
    assert match_chain(None, "Supermercado Diagonal") is None  # 'dia' inside 'diagonal'


# --------------------------------------------------------------------------- #
# fetch_locations: ES-only client-side filter + pagination
# --------------------------------------------------------------------------- #
def test_fetch_locations_filters_es_and_paginates() -> None:
    adapter = _discover_adapter([_LOCATIONS[:5], _LOCATIONS[5:]])
    locs = adapter.fetch_locations("ES")
    ids = {loc.osm_id for loc in locs}
    assert 8 not in ids  # FR filtered out client-side
    assert {1, 2, 3, 4, 5, 6, 7, 9, 10} <= ids


# --------------------------------------------------------------------------- #
# discover(): upserts only priced ES stores of the 6 chains; idempotent
# --------------------------------------------------------------------------- #
#: The 6 chain locations map to these external codes (osm ids 1..6); the excluded ones (Deza=7,
#: FR=8, zero-price=9, Media Markt=10) must never become stores.
_CHAIN_CODES = {f"osm:NODE/{i}" for i in range(1, 7)}
_EXCLUDED_CODES = {f"osm:NODE/{i}" for i in (7, 8, 9, 10)}


def _discovered_stores(db: Session, codes: set[str]) -> list[Store]:
    return list(
        db.execute(select(Store).where(Store.external_code.in_(codes))).scalars().all()
    )


def test_discover_seeds_only_priced_es_chain_stores(db_session: Session) -> None:
    adapter = _discover_adapter([_LOCATIONS])
    per_chain = discover(db_session, adapter=adapter)

    # Every one of the 6 chains discovered exactly its single priced ES store.
    for chain in ("Mercadona", "Aldi", "Lidl", "Carrefour", "Dia", "Alcampo"):
        assert per_chain[chain]["discovered"] == 1, chain
        assert per_chain[chain]["created"] == 1, chain

    # The 6 chain stores are real (is_synthetic=False); Deza/FR/zero-price/Media Markt never
    # become stores.
    chain_stores = _discovered_stores(db_session, _CHAIN_CODES)
    assert {s.external_code for s in chain_stores} == _CHAIN_CODES
    assert all(s.is_synthetic is False for s in chain_stores)
    assert _discovered_stores(db_session, _EXCLUDED_CODES) == []


def test_discover_is_idempotent(db_session: Session) -> None:
    discover(db_session, adapter=_discover_adapter([_LOCATIONS]))
    first = _discovered_stores(db_session, _CHAIN_CODES)
    per_chain = discover(db_session, adapter=_discover_adapter([_LOCATIONS]))
    second = _discovered_stores(db_session, _CHAIN_CODES)

    assert len(first) == len(second) == 6  # no duplicate stores on re-run
    for chain in ("Mercadona", "Aldi", "Lidl", "Carrefour", "Dia", "Alcampo"):
        assert per_chain[chain]["created"] == 0
        assert per_chain[chain]["existing"] == 1
