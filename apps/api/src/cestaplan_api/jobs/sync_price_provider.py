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
from datetime import UTC, datetime

from sqlalchemy import func, select

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.contracts import ProductQuery
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import CrawlRun, Retailer
from cestaplan_api.services.provider_sync import SyncMode, run_provider_sync


def _last_price_crawl_age_days(db, retailer_id: int) -> float | None:
    """Age (in days) of the most recent completed price crawl for the retailer, or None."""
    last = db.execute(
        select(func.max(CrawlRun.created_at)).where(
            CrawlRun.retailer_id == retailer_id,
            CrawlRun.run_type == "prices",
            CrawlRun.status == "completed",
        )
    ).scalar()
    if last is None:
        return None
    return (datetime.now(UTC) - last).total_seconds() / 86400.0


def run(
    provider_code: str,
    retailer_slug: str | None,
    mode: SyncMode,
    limit: int | None,
    min_age_days: float | None = None,
) -> int:
    if not registry.has(provider_code):
        print(f"Proveedor desconocido: {provider_code!r} (conocidos: {registry.codes()})")
        return 1
    with SessionLocal() as db:
        provider = registry.get(provider_code)
        # Default the retailer to the provider's own declared retailer (metadata) when omitted.
        slug = retailer_slug or provider.get_source_metadata().retailer_slug
        retailer = db.execute(select(Retailer).where(Retailer.slug == slug)).scalars().first()
        if retailer is None:
            print(f"Retailer no encontrado: {slug!r}")
            return 1
        # Cadence gate: skip the crawl entirely when a recent one already exists. Lets a daily
        # trigger stay idempotent — it only actually crawls when the monthly refresh is due.
        if min_age_days is not None:
            age = _last_price_crawl_age_days(db, retailer.id)
            if age is not None and age < min_age_days:
                print(json.dumps({
                    "skipped": True, "reason": "recent_crawl",
                    "age_days": round(age, 1), "min_age_days": min_age_days,
                }, ensure_ascii=False))
                return 0
        report = run_provider_sync(
            db,
            provider,
            retailer,
            get_settings(),
            mode=mode,
            query=ProductQuery(max_products=limit),
        )
        # A quarantined run wrote nothing (bad crawl never replaces good prices): roll back
        # so an empty/aborted production run leaves the live catalogue untouched.
        if mode is SyncMode.DRY_RUN or report.quarantined:
            db.rollback()
        else:
            db.commit()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza precios de un proveedor externo.")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--retailer", default=None)  # defaults to the provider's own retailer
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--staging-import", action="store_true")
    # Explicit, auditable production refresh. Still passes through the activation gate
    # (guard_production_sync): it only writes when the provider is human-approved for
    # production and the crawl quality is accepted. Never selected implicitly.
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    # Cadence gate for automated triggers: skip the crawl if the last completed one is younger
    # than this many days (so a daily trigger only crawls when the monthly refresh is due).
    parser.add_argument("--min-age-days", type=float, default=None)
    args = parser.parse_args()
    if args.production:
        mode = SyncMode.PRODUCTION
    elif args.staging_import:
        mode = SyncMode.STAGING
    elif args.dry_run:
        mode = SyncMode.DRY_RUN
    else:
        mode = SyncMode.DRY_RUN  # safe default: never hit production implicitly
    raise SystemExit(run(args.provider, args.retailer, mode, args.limit, args.min_age_days))


if __name__ == "__main__":
    main()
