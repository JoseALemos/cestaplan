"""Seed the real Open Prices chains + stores (Task 2).

Creates a REAL ``Retailer`` per Spanish chain that Open Prices has data for (Mercadona,
Aldi, Lidl, Carrefour, Dia, Alcampo — **not** Deza) and a REAL ``Store`` per OSM location,
plus the Open Prices ``DataSource`` (ODbL). Everything is ``is_synthetic=False``. Stores
start with NO prices — real prices are pulled later by the sync command; nothing is
fabricated here.

Idempotent: retailers upsert by ``slug`` and stores upsert by ``(retailer, external_code)``
where ``external_code = osm:{TYPE}/{osm_id}``. Running it repeatedly yields identical rows.

Two paths:

- **default (no flag)** — seed the small embedded list (offline, deterministic; used by tests).
- ``--discover`` — the network path: query the live Open Prices ``/locations`` (paginate ALL,
  filter client-side to ``osm_address_country_code == 'ES'``) and upsert a real ``Store`` for
  every location of the 6 chains that has ``price_count > 0``. Idempotent (upsert by
  ``external_code``); reports how many stores were discovered per chain.

Run::

    uv run python -m cestaplan_api.scripts.seed_open_prices_stores
    uv run python -m cestaplan_api.scripts.seed_open_prices_stores --discover
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import (
    OP_ADAPTER_KEY,
    OpenPricesAdapter,
    OpenPricesLocation,
)
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Retailer, Store
from cestaplan_api.scripts.open_prices_stores_data import (
    OPEN_PRICES_CHAINS,
    OPEN_PRICES_STORES,
    match_chain,
)
from cestaplan_api.services.open_prices_sync import (
    ensure_open_prices_data_source,
    store_external_code,
)


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _upsert_retailer(session: Session, name: str, slug: str) -> tuple[Retailer, bool]:
    retailer = session.execute(
        select(Retailer).where(Retailer.slug == slug)
    ).scalar_one_or_none()
    if retailer is not None:
        # Keep it linked to Open Prices and real; never flip an existing row to synthetic.
        retailer.adapter_key = OP_ADAPTER_KEY
        retailer.is_synthetic = False
        retailer.is_active = True
        return retailer, False
    retailer = Retailer(
        slug=slug,
        name=name,
        adapter_key=OP_ADAPTER_KEY,
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    session.add(retailer)
    session.flush()
    return retailer, True


def _upsert_store(
    session: Session, retailer: Retailer, loc: dict[str, object]
) -> tuple[Store, bool]:
    external_code = store_external_code(str(loc["osm_type"]), int(loc["osm_id"]))  # type: ignore[arg-type]
    store = session.execute(
        select(Store).where(
            Store.retailer_id == retailer.id, Store.external_code == external_code
        )
    ).scalar_one_or_none()
    created = store is None
    if store is None:
        store = Store(retailer_id=retailer.id, external_code=external_code)
        session.add(store)
    store.name = str(loc["name"])
    store.locality = str(loc["city"])
    store.postal_code = str(loc["pc"])
    store.latitude = _d(loc["lat"])
    store.longitude = _d(loc["lon"])
    store.is_active = True
    store.is_synthetic = False
    session.flush()
    return store, created


def _upsert_store_from_location(
    session: Session, retailer: Retailer, loc: OpenPricesLocation
) -> tuple[Store, bool]:
    """Upsert a real ``Store`` from a discovered Open Prices location (by external_code)."""
    external_code = store_external_code(loc.osm_type, loc.osm_id)
    store = session.execute(
        select(Store).where(
            Store.retailer_id == retailer.id, Store.external_code == external_code
        )
    ).scalar_one_or_none()
    created = store is None
    if store is None:
        store = Store(retailer_id=retailer.id, external_code=external_code)
        session.add(store)
    if loc.osm_name:
        store.name = loc.osm_name
    if loc.city:
        store.locality = loc.city
    if loc.postcode:
        store.postal_code = loc.postcode
    if loc.latitude is not None:
        store.latitude = loc.latitude
    if loc.longitude is not None:
        store.longitude = loc.longitude
    store.is_active = True
    store.is_synthetic = False
    session.flush()
    return store, created


def discover(
    session: Session, *, adapter: OpenPricesAdapter | None = None
) -> dict[str, dict[str, int]]:
    """Live discovery: seed every priced ES store of the 6 chains from Open Prices ``/locations``.

    Paginates ALL locations, keeps the ES ones with ``price_count > 0`` that map to a supported
    chain (never Deza), and upserts a real ``Retailer`` + ``Store`` per location. Idempotent by
    ``external_code``. Returns per-chain ``{discovered, created, existing}`` counts.
    """
    adapter = adapter or OpenPricesAdapter()
    ensure_open_prices_data_source(session)
    per_chain: dict[str, dict[str, int]] = {
        chain: {"discovered": 0, "created": 0, "existing": 0} for chain in OPEN_PRICES_CHAINS
    }
    retailers: dict[str, Retailer] = {}
    for loc in adapter.fetch_locations("ES"):
        if loc.price_count <= 0:
            continue
        chain = match_chain(loc.osm_brand, loc.osm_name)
        if chain is None:
            continue
        retailer = retailers.get(chain)
        if retailer is None:
            retailer, _ = _upsert_retailer(session, chain, OPEN_PRICES_CHAINS[chain])
            retailers[chain] = retailer
        _store, created = _upsert_store_from_location(session, retailer, loc)
        per_chain[chain]["discovered"] += 1
        per_chain[chain]["created" if created else "existing"] += 1
    return per_chain


def seed(session: Session) -> dict[str, int]:
    counts = {
        "retailers_created": 0,
        "retailers_existing": 0,
        "stores_created": 0,
        "stores_existing": 0,
    }
    ensure_open_prices_data_source(session)
    for chain_name, slug in OPEN_PRICES_CHAINS.items():
        retailer, r_created = _upsert_retailer(session, chain_name, slug)
        counts["retailers_created" if r_created else "retailers_existing"] += 1
        for loc in OPEN_PRICES_STORES.get(chain_name, []):
            _store, s_created = _upsert_store(session, retailer, loc)
            counts["stores_created" if s_created else "stores_existing"] += 1
    return counts


def _run_discover() -> None:
    with SessionLocal() as session:
        per_chain = discover(session)
        session.commit()

    total = sum(c["discovered"] for c in per_chain.values())
    print("CestaPlan Open Prices discovery — live ES stores of the 6 chains (is_synthetic=False)")
    for chain, counts in per_chain.items():
        print(
            f"  {chain:<11}: discovered={counts['discovered']} "
            f"created={counts['created']} existing={counts['existing']}"
        )
    print(f"  total discovered  : {total} priced ES store(s)")
    print("  Stores hold NO prices yet; run sync_all_sources to pull real Open Prices + enrich.")


def _run_seed() -> None:
    with SessionLocal() as session:
        counts = seed(session)
        session.commit()

    print("CestaPlan Open Prices seed — real chains + stores (is_synthetic=False)")
    print(f"  chains          : {', '.join(OPEN_PRICES_CHAINS)}")
    print(f"  retailers created : {counts['retailers_created']}")
    print(f"  retailers existing: {counts['retailers_existing']}")
    print(f"  stores created    : {counts['stores_created']}")
    print(f"  stores existing   : {counts['stores_existing']}")
    print("  Stores start with NO prices; run sync_open_prices to pull real Open Prices.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Siembra cadenas/tiendas reales de Open Prices (ES)."
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Descubre en vivo TODAS las tiendas ES con precios de las 6 cadenas "
        "(consulta /locations de Open Prices). Sin la bandera usa la lista embebida.",
    )
    args = parser.parse_args()
    if args.discover:
        _run_discover()
    else:
        _run_seed()


if __name__ == "__main__":
    main()
