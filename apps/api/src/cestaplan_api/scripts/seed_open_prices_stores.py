"""Seed the real Open Prices chains + stores (Task 2).

Creates a REAL ``Retailer`` per Spanish chain that Open Prices has data for (Mercadona,
Aldi, Lidl, Carrefour, Dia, Alcampo — **not** Deza) and a REAL ``Store`` per OSM location,
plus the Open Prices ``DataSource`` (ODbL). Everything is ``is_synthetic=False``. Stores
start with NO prices — real prices are pulled later by the sync command; nothing is
fabricated here.

Idempotent: retailers upsert by ``slug`` and stores upsert by ``(retailer, external_code)``
where ``external_code = osm:{TYPE}/{osm_id}``. Running it repeatedly yields identical rows.

Run::

    uv run python -m cestaplan_api.scripts.seed_open_prices_stores
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OP_ADAPTER_KEY
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Retailer, Store
from cestaplan_api.scripts.open_prices_stores_data import (
    OPEN_PRICES_CHAINS,
    OPEN_PRICES_STORES,
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


def main() -> None:
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


if __name__ == "__main__":
    main()
