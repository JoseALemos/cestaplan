"""CLI: re-queue the failed / dead-lettered jobs of a crawl run.

    python -m cestaplan_api.jobs.retry_failed --run-id <uuid>
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion import JobStatus
from cestaplan_api.models import CrawlJob, CrawlRun

_RETRYABLE = (JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value)


def run(run_public_id: str) -> int:
    try:
        run_uuid = uuid.UUID(run_public_id)
    except ValueError:
        print(f"run-id no es un UUID válido: {run_public_id!r}")
        return 1

    with SessionLocal() as db:
        crawl_run = db.execute(
            select(CrawlRun).where(CrawlRun.public_id == run_uuid)
        ).scalars().first()
        if crawl_run is None:
            print(f"Crawl run no encontrado: {run_public_id}")
            return 1

        jobs = list(
            db.execute(
                select(CrawlJob).where(
                    CrawlJob.crawl_run_id == crawl_run.id,
                    CrawlJob.status.in_(_RETRYABLE),
                )
            ).scalars()
        )
        now = datetime.now(UTC)
        for job in jobs:
            job.status = JobStatus.QUEUED.value
            job.attempts = 0
            job.available_at = now
            job.locked_at = None
            job.locked_by = None
            job.heartbeat_at = None
            job.last_error = None
        db.commit()

    print(f"CestaPlan — reintento de jobs fallidos del run {run_public_id}")
    print(f"  jobs_reencolados={len(jobs)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reencola los jobs fallidos / dead-letter de un crawl run."
    )
    parser.add_argument("--run-id", required=True, help="public_id (UUID) del crawl run")
    args = parser.parse_args()
    raise SystemExit(run(args.run_id))


if __name__ == "__main__":
    main()
