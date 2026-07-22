"""Sync everything from the open sources (the daily job / Railway cron entry point).

Runs the whole open-data refresh in order:

1. **Open Prices** — pull real prices for every Open-Prices-linked store (idempotent,
   append-only, ODbL).
2. **Open Food Facts** — enrich the resulting real products with nutrition/allergens/brand/
   image/category (data only, **never prices**), skipping products already enriched.

Both steps are gated by their ``DataSource.is_enabled`` flag (a disabled source is skipped).
Prints a combined per-chain summary. Respectful timeouts/rate live in the adapters.

Run::

    python -m cestaplan_api.scripts.sync_all_sources
    uv run python -m cestaplan_api.scripts.sync_all_sources
"""

from __future__ import annotations

import argparse

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.commercial_feed_sync import CommercialFeedRun
from cestaplan_api.services.commercial_feed_sync import sync_all as sync_commercial_feed_all
from cestaplan_api.services.ingredient_matching import (
    MappingSummary,
    all_chain_coverage,
    map_real_products,
)
from cestaplan_api.services.open_prices_sync import (
    OrchestrationSummary,
    sync_all_and_enrich,
)


def _print_summary(result: OrchestrationSummary) -> None:
    print("CestaPlan — sincronización de todas las fuentes abiertas")
    print(
        f"  Open Prices    : {'activo' if result.open_prices_enabled else 'DESHABILITADO'}"
    )
    print(
        f"  Open Food Facts: {'activo' if result.openfoodfacts_enabled else 'DESHABILITADO'}"
    )
    print(
        f"  tiendas={result.stores_synced} "
        f"precios_consultados={result.prices_fetched} "
        f"precios_nuevos={result.prices_inserted} "
        f"productos_nuevos={result.products_created} "
        f"productos_enriquecidos={result.products_enriched}"
    )
    for chain, counts in result.per_chain.items():
        print(
            f"    {chain:<11}: tiendas={counts['stores']} "
            f"precios_nuevos={counts['prices_inserted']} "
            f"enriquecidos={counts['products_enriched']}"
        )
    print(f"  {result.attribution}")
    print(
        "  Datos de Open Food Facts bajo ODbL 1.0. https://openfoodfacts.org"
    )


def _print_commercial(result: CommercialFeedRun) -> None:
    if not result.enabled:
        return
    print(
        f"  Feed comercial : tiendas={result.stores_synced} "
        f"precios_nuevos={result.prices_inserted} "
        f"productos_nuevos={result.products_created} "
        f"productos_enriquecidos={result.products_enriched}"
    )
    if result.attribution:
        print(f"    {result.attribution}")


def _print_mapping(result: MappingSummary) -> None:
    print(
        f"  Mapeo ingred.  : analizados={result.scanned} "
        f"mapeados={result.mapped} sin_coincidencia={result.unmatched}"
    )
    for cov in result.chain_coverage:
        print(
            f"    {cov['chain']:<11}: "
            f"{cov['priced_ingredients']}/{cov['total_ingredients']} "
            "ingredientes con precio"
        )


def run(*, enrich: bool = True) -> int:
    with SessionLocal() as session:
        result = sync_all_and_enrich(session, enrich=enrich)
        # The opt-in commercial feed only runs when enabled + configured (no-op otherwise).
        commercial = sync_commercial_feed_all(session, enrich=enrich)
        # Map freshly-synced real products to canonical ingredients (after OFF enrichment, so
        # OFF categories/names are available). Conservative + idempotent; never fabricates.
        mapping = map_real_products(session)
        mapping.chain_coverage = all_chain_coverage(session)
        session.commit()
    _print_summary(result)
    _print_commercial(commercial)
    _print_mapping(mapping)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincroniza precios (Open Prices) y enriquece productos (Open Food Facts)."
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
