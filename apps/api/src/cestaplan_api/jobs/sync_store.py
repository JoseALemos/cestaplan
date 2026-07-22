"""CLI: force-schedule crawl runs + jobs for a single store, now.

    python -m cestaplan_api.jobs.sync_store --store-id <uuid>
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.scheduler import CrawlScheduler
from cestaplan_api.models import Store


def run(store_public_id: str) -> int:
    try:
        store_uuid = uuid.UUID(store_public_id)
    except ValueError:
        print(f"store-id no es un UUID válido: {store_public_id!r}")
        return 1

    with SessionLocal() as db:
        store = db.execute(
            select(Store).where(Store.public_id == store_uuid)
        ).scalars().first()
        if store is None:
            print(f"Store no encontrada: {store_public_id}")
            return 1
        report = CrawlScheduler().schedule_store(db, store, force=True)
        db.commit()

    print(f"CestaPlan — sincronización forzada de la store {store_public_id}")
    print(f"  runs_creados={report.runs_created} jobs_creados={report.jobs_created}")
    for run_id in report.run_public_ids:
        print(f"    run {run_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Programa inmediatamente los crawl jobs de una store."
    )
    parser.add_argument("--store-id", required=True, help="public_id (UUID) de la store")
    args = parser.parse_args()
    raise SystemExit(run(args.store_id))


if __name__ == "__main__":
    main()
