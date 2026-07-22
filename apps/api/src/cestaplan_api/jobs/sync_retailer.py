"""CLI: force-schedule crawl runs + jobs for a single retailer, now.

    python -m cestaplan_api.jobs.sync_retailer --retailer mercadona
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.scheduler import CrawlScheduler
from cestaplan_api.models import Retailer


def run(retailer_code: str) -> int:
    with SessionLocal() as db:
        retailer = db.execute(
            select(Retailer).where(Retailer.slug == retailer_code)
        ).scalars().first()
        if retailer is None:
            print(f"Retailer no encontrado: {retailer_code!r}")
            return 1
        report = CrawlScheduler().schedule_retailer(db, retailer, force=True)
        db.commit()

    print(f"CestaPlan — sincronización forzada del retailer {retailer_code}")
    print(
        f"  runs_creados={report.runs_created} jobs_creados={report.jobs_created}"
    )
    for run_id in report.run_public_ids:
        print(f"    run {run_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Programa inmediatamente los crawl jobs de un retailer."
    )
    parser.add_argument("--retailer", required=True, help="slug del retailer (p.ej. mercadona)")
    args = parser.parse_args()
    raise SystemExit(run(args.retailer))


if __name__ == "__main__":
    main()
