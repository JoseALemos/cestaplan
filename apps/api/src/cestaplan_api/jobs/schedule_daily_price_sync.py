"""CLI: run the daily crawl scheduler (create today's crawl runs + jobs, idempotently).

    python -m cestaplan_api.jobs.schedule_daily_price_sync
"""

from __future__ import annotations

import argparse

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.scheduler import CrawlScheduler


def run() -> int:
    with SessionLocal() as db:
        report = CrawlScheduler().schedule_daily(db)
        db.commit()

    print("CestaPlan — planificación diaria de rastreo de precios")
    if not report.acquired_lock:
        print("  Otro planificador está en ejecución (lock no adquirido); nada que hacer.")
        return 0
    print(
        f"  runs_creados={report.runs_created} "
        f"jobs_creados={report.jobs_created} "
        f"omitidos_existentes={report.skipped_existing} "
        f"retailers_omitidos={report.skipped_retailers}"
    )
    for run_id in report.run_public_ids:
        print(f"    run {run_id}")
    return 0


def main() -> None:
    argparse.ArgumentParser(
        description="Crea los crawl runs + jobs del día (idempotente)."
    ).parse_args()
    raise SystemExit(run())


if __name__ == "__main__":
    main()
