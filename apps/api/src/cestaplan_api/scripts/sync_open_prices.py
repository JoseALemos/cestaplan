"""Sync real prices from Open Prices (Task 3 command — the cron entry point).

Pulls Open Prices observations for the Open-Prices-linked stores and appends new price
observations (idempotent, append-only). This is what a scheduled Railway cron / system cron
invokes daily; it hits the live Open Prices API (read-only, ODbL).

Run::

    python -m cestaplan_api.scripts.sync_open_prices --all
    python -m cestaplan_api.scripts.sync_open_prices --store <store_public_id>
    uv run python -m cestaplan_api.scripts.sync_open_prices --all
"""

from __future__ import annotations

import argparse
import sys
import uuid

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import Store
from cestaplan_api.services.open_prices_sync import (
    SyncSummary,
    open_prices_enabled,
    open_prices_stores,
    sync_store,
)


def _print_summary(summary: SyncSummary) -> None:
    print(
        f"  store {summary.store_public_id} "
        f"[osm:{summary.osm_type}/{summary.osm_id}]: "
        f"fetched={summary.fetched} inserted={summary.inserted} "
        f"skipped_existing={summary.skipped_existing} "
        f"no_barcode={summary.skipped_no_barcode} "
        f"products_new={summary.products_created}"
    )
    for err in summary.errors:
        print(f"    ! {err}")


def run(*, all_stores: bool, store_public_id: str | None) -> int:
    with SessionLocal() as session:
        if not open_prices_enabled(session):
            session.commit()
            print("La fuente Open Prices está deshabilitada; nada que sincronizar.")
            return 0

        if store_public_id is not None:
            try:
                pid = uuid.UUID(store_public_id)
            except ValueError:
                print(f"store id no es un UUID válido: {store_public_id!r}", file=sys.stderr)
                return 2
            store = session.execute(
                select(Store).where(Store.public_id == pid)
            ).scalar_one_or_none()
            if store is None:
                print(f"No existe la tienda {store_public_id}.", file=sys.stderr)
                return 1
            stores = [store]
        else:
            stores = open_prices_stores(session)

        summaries = [sync_store(session, store) for store in stores]
        session.commit()

    total_inserted = sum(s.inserted for s in summaries)
    total_fetched = sum(s.fetched for s in summaries)
    print(
        f"Open Prices sync — {len(summaries)} tienda(s), "
        f"{total_fetched} precios consultados, {total_inserted} observaciones nuevas."
    )
    for summary in summaries:
        _print_summary(summary)
    print(
        "Precios de Open Food Facts - Open Prices, bajo licencia ODbL. "
        "https://prices.openfoodfacts.org"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza precios reales desde Open Food Facts - Open Prices."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all", action="store_true", help="Sincroniza todas las tiendas de Open Prices."
    )
    group.add_argument(
        "--store", metavar="PUBLIC_ID", help="Sincroniza una sola tienda por su id público."
    )
    args = parser.parse_args()
    raise SystemExit(run(all_stores=args.all, store_public_id=args.store))


if __name__ == "__main__":
    main()
