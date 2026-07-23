"""CLI: run a price-provider sync in dry-run / staging / production mode (spec §P).

    python -m cestaplan_api.jobs.sync_price_provider --provider open-prices \
        --retailer dia --dry-run --limit 10
    python -m cestaplan_api.jobs.sync_price_provider --provider demo \
        --retailer mercaejemplo --staging-import

Dry-run is the default (writes nothing). Production requires the activation gate (§O).
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.contracts import ProductQuery
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import Retailer
from cestaplan_api.services.provider_sync import SyncMode, run_provider_sync


def run(provider_code: str, retailer_slug: str, mode: SyncMode, limit: int | None) -> int:
    if not registry.has(provider_code):
        print(f"Proveedor desconocido: {provider_code!r} (conocidos: {registry.codes()})")
        return 1
    with SessionLocal() as db:
        retailer = (
            db.execute(select(Retailer).where(Retailer.slug == retailer_slug)).scalars().first()
        )
        if retailer is None:
            print(f"Retailer no encontrado: {retailer_slug!r}")
            return 1
        provider = registry.get(provider_code)
        report = run_provider_sync(
            db,
            provider,
            retailer,
            get_settings(),
            mode=mode,
            query=ProductQuery(max_products=limit),
        )
        if mode is not SyncMode.DRY_RUN:
            db.commit()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza precios de un proveedor externo.")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--retailer", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--staging-import", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.staging_import:
        mode = SyncMode.STAGING
    elif args.dry_run:
        mode = SyncMode.DRY_RUN
    else:
        mode = SyncMode.DRY_RUN  # safe default: never hit production implicitly
    raise SystemExit(run(args.provider, args.retailer, mode, args.limit))


if __name__ == "__main__":
    main()
