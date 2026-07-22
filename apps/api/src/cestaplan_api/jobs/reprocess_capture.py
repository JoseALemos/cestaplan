"""CLI: enqueue a reprocess job for a stored raw capture (parse/normalize re-run).

Thin stub for FASE A: it creates a fresh crawl run for the capture's retailer/store and
enqueues a ``reprocess`` job pointing at the capture. The actual re-parse/normalize is
performed later by the connector handler registered for the ``reprocess`` job type.

    python -m cestaplan_api.jobs.reprocess_capture --capture-id <uuid>
"""

from __future__ import annotations

import argparse
import uuid

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion import RunType
from cestaplan_api.ingestion.run_service import CrawlRunService, JobSpec
from cestaplan_api.models import RawCapture

_REPROCESS_JOB_TYPE = "reprocess"


def run(capture_public_id: str) -> int:
    try:
        capture_uuid = uuid.UUID(capture_public_id)
    except ValueError:
        print(f"capture-id no es un UUID válido: {capture_public_id!r}")
        return 1

    with SessionLocal() as db:
        capture = db.execute(
            select(RawCapture).where(RawCapture.public_id == capture_uuid)
        ).scalars().first()
        if capture is None:
            print(f"Raw capture no encontrada: {capture_public_id}")
            return 1

        service = CrawlRunService(db)
        crawl_run = service.create_run(
            retailer_id=capture.retailer_id,
            store_id=capture.store_id,
            run_type=RunType.PRICES,
            parser_version=capture.parser_version,
        )
        spec = JobSpec(
            job_type=_REPROCESS_JOB_TYPE,
            payload={
                "capture_public_id": str(capture.public_id),
                "raw_capture_id": capture.id,
                "source_url": capture.source_url,
            },
            idempotency_key=f"reprocess:{capture.public_id}:{crawl_run.public_id}",
        )
        service.enqueue_jobs(crawl_run, [spec])
        db.commit()
        run_public_id = str(crawl_run.public_id)

    print(f"CestaPlan — reprocesado de la captura {capture_public_id}")
    print(f"  run_creado={run_public_id} job=reprocess")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reencola el parse/normalize de una captura almacenada."
    )
    parser.add_argument("--capture-id", required=True, help="public_id (UUID) de la captura")
    args = parser.parse_args()
    raise SystemExit(run(args.capture_id))


if __name__ == "__main__":
    main()
