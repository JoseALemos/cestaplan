"""Sync prices from an authorized commercial feed (cron entry point).

Pulls the licensed third-party price feed for every commercial-feed-linked store and appends
new price observations (idempotent, append-only). This is what a scheduled Railway cron / system
cron invokes; it consumes an authorized API with the operator's key (no scraping).

**Disabled by default.** Runs only when the ``commercial-feed`` ``DataSource.is_enabled`` flag is
on AND the connector is configured (``COMMERCIAL_FEED_BASE_URL`` / ``COMMERCIAL_FEED_API_KEY`` /
``COMMERCIAL_FEED_MAPPING``). Otherwise it prints a clear message and exits without writing.

Run::

    python -m cestaplan_api.scripts.sync_commercial_feed --all
    uv run python -m cestaplan_api.scripts.sync_commercial_feed --all
"""

from __future__ import annotations

import argparse

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.commercial_feed_sync import CommercialFeedRun, sync_all


def _print_summary(result: CommercialFeedRun) -> None:
    print("CestaPlan — sincronización del feed comercial (authorized_partner)")
    print(f"  configurado: {'sí' if result.configured else 'NO'}")
    print(f"  habilitado : {'sí' if result.enabled else 'NO'}")
    print(
        f"  tiendas={result.stores_synced} "
        f"precios_consultados={result.prices_fetched} "
        f"precios_nuevos={result.prices_inserted} "
        f"productos_nuevos={result.products_created} "
        f"productos_enriquecidos={result.products_enriched}"
    )
    if result.attribution:
        print(f"  {result.attribution}")


def run(*, enrich: bool = True) -> int:
    with SessionLocal() as session:
        result = sync_all(session, enrich=enrich)
        session.commit()
    if not result.enabled:
        reason = "no configurado" if not result.configured else "deshabilitado"
        print(f"El feed comercial está {reason}; nada que sincronizar.")
        return 0
    _print_summary(result)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza precios desde un feed comercial autorizado (authorized_partner)."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Sincroniza todas las tiendas enlazadas al feed comercial (por defecto).",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Solo sincroniza precios; omite el enriquecimiento con Open Food Facts.",
    )
    args = parser.parse_args()
    raise SystemExit(run(enrich=not args.no_enrich))


if __name__ == "__main__":
    main()
