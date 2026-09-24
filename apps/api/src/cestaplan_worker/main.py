"""CestaPlan job-queue worker loop.

Run with::

    python -m cestaplan_worker.main

Polls :class:`GenerationJob` with ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple
workers never grab the same job. A claimed job is marked ``collecting_data`` +
locked (``locked_by`` / ``locked_at`` / ``heartbeat_at``) before the row lock is
released, then processed by :func:`process_job`.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.models import GenerationJob, MealPlan, OptimizationRun
from cestaplan_worker.processor import process_job

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


# The always-on generation worker doubles as the trigger for the monthly Mercadona price
# refresh (there is no separate cron service). Once a day it fires a cadence-aware, ISOLATED
# subprocess that self-skips (via --min-age-days) unless a refresh is actually due, so plan
# generation is never blocked and a crawl failure can never crash this loop.
_PRICE_REFRESH_CHECK_INTERVAL = timedelta(hours=24)
_PRICE_REFRESH_MIN_AGE_DAYS = 28
_price_refresh_state: dict[str, datetime | None] = {"last_check": None}


def _maybe_refresh_prices(settings, now: datetime | None = None) -> None:
    """Once a day, trigger the cadence-aware Mercadona price refresh in a detached subprocess.

    Best-effort maintenance: any failure is logged and swallowed so the job loop is unaffected.
    The subprocess itself enforces the monthly cadence and the production activation gate.
    """
    now = now or _now()
    if not getattr(settings, "mercadona_connector_enabled", False):
        return
    last = _price_refresh_state["last_check"]
    if last is not None and (now - last) < _PRICE_REFRESH_CHECK_INTERVAL:
        return
    _price_refresh_state["last_check"] = now
    try:
        subprocess.Popen(
            [
                sys.executable, "-m", "cestaplan_api.jobs.sync_price_provider",
                "--provider", "apify-mercadona", "--retailer", "mercadona",
                "--production", "--min-age-days", str(_PRICE_REFRESH_MIN_AGE_DAYS),
            ],
            start_new_session=True,  # detached: survives a worker restart, never blocks the loop
        )
        logger.info("price refresh: triggered cadence-aware Mercadona sync subprocess")
    except Exception:
        logger.warning("price refresh: could not spawn sync subprocess", exc_info=True)


# Estados NO terminales en los que un job puede quedar colgado si el worker muere (SIGKILL de
# Railway tras el grace, OOM). Terminales: completed/failed/cancelled. queued lo retoma claim_job.
_IN_PROGRESS_STATUSES = ("collecting_data", "generating_candidates", "validating", "optimizing")


def _fail_linked(db: Session, job: GenerationJob, now: datetime) -> None:
    """Sincroniza a 'failed' el run/plan enlazados de un job dead-lettered (para que la UI no quede
    en 'en curso' para siempre). Espeja lo que hace processor._handle_failure al agotar intentos."""
    if job.optimization_run_id is not None:
        run = db.get(OptimizationRun, job.optimization_run_id)
        if run is not None:
            run.status = "failed"
            run.finished_at = now
    if job.meal_plan_id is not None:
        plan = db.get(MealPlan, job.meal_plan_id)
        if plan is not None:
            plan.status = "failed"


def recover_abandoned_jobs(
    db: Session, *, now: datetime | None = None, timeout: timedelta | None = None
) -> int:
    """Re-encola (o dead-lettea) los GenerationJob abandonados por un worker muerto. Devuelve nº.

    Un job en un estado no terminal con el heartbeat vencido pertenece a una instancia de worker
    que ya no vive; ``claim_job`` solo mira ``queued``, así que nadie lo retomaría y quedaría
    colgado para siempre (feature central: la generación de planes). Se llama al ARRANCAR el worker
    (mismo patrón que ``crawl_worker.recover_abandoned``): en single-replica la instancia previa ya
    está muerta, así que no hay riesgo de doble-procesamiento; el ``timeout`` generoso protege
    además de un solape efímero durante un redeploy. Cada recuperación cuenta como un intento
    (espeja ``_handle_failure``): si agota ``max_attempts`` se dead-lettea a ``failed``.
    """
    now = now or _now()
    if timeout is None:
        timeout = timedelta(seconds=max(get_settings().worker_heartbeat_seconds * 40, 600))
    cutoff = now - timeout
    jobs = (
        db.execute(
            select(GenerationJob)
            .where(
                GenerationJob.status.in_(_IN_PROGRESS_STATUSES),
                or_(
                    GenerationJob.heartbeat_at.is_(None),
                    GenerationJob.heartbeat_at < cutoff,
                ),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job.attempts += 1
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.last_error = "reaped: el worker abandonó el job (heartbeat vencido)"
            _fail_linked(db, job, now)
        else:
            job.status = "queued"
            job.run_after = None
    db.flush()
    if jobs:
        logger.warning("reaper: recuperados %d GenerationJob abandonados", len(jobs))
    return len(jobs)


def claim_job(
    db: Session, worker_id: str, now: datetime | None = None
) -> GenerationJob | None:
    """Atomically claim the next runnable job (FOR UPDATE SKIP LOCKED).

    Sets the lock fields and moves the job out of ``queued`` so it is not re-claimed
    once the row lock is released. The caller controls the surrounding transaction.
    """
    now = now or _now()
    job = db.execute(
        select(GenerationJob)
        .where(
            GenerationJob.status == "queued",
            or_(GenerationJob.run_after.is_(None), GenerationJob.run_after <= now),
        )
        .order_by(GenerationJob.priority.desc(), GenerationJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalars().first()
    if job is None:
        return None

    job.status = "collecting_data"
    job.locked_at = now
    job.locked_by = worker_id
    job.heartbeat_at = now
    db.flush()
    return job


def run_worker(
    worker_id: str | None = None,
    *,
    stop: object | None = None,
    max_idle_loops: int | None = None,
) -> None:
    """Poll and process jobs until ``stop`` is set (or ``max_idle_loops`` idle polls)."""
    settings = get_settings()
    worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    interval = settings.worker_poll_interval_seconds
    idle = 0

    def _should_stop() -> bool:
        return bool(stop) and bool(getattr(stop, "is_set", lambda: False)())

    # Al arrancar, recupera jobs que una instancia anterior dejó colgados (redeploy/OOM/SIGKILL).
    db = SessionLocal()
    try:
        recover_abandoned_jobs(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("reaper: fallo recuperando jobs abandonados al arrancar", exc_info=True)
    finally:
        db.close()

    while not _should_stop():
        _maybe_refresh_prices(settings)
        processed = _poll_once(worker_id)
        if processed:
            idle = 0
            continue
        idle += 1
        if max_idle_loops is not None and idle >= max_idle_loops:
            return
        time.sleep(interval)


def _poll_once(worker_id: str) -> bool:
    """Claim and process at most one job. Returns True if a job was processed."""
    db = SessionLocal()
    try:
        job = claim_job(db, worker_id)
        if job is None:
            db.commit()
            return False
        # Release the row lock; the job is now non-queued so no one re-claims it.
        db.commit()
        process_job(job, db)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    stop = _StopFlag()
    signal.signal(signal.SIGINT, stop.set_from_signal)
    signal.signal(signal.SIGTERM, stop.set_from_signal)
    run_worker(stop=stop)


class _StopFlag:
    def __init__(self) -> None:
        self._stop = False

    def is_set(self) -> bool:
        return self._stop

    def __bool__(self) -> bool:  # allow `bool(stop)` to reflect truthiness of existence
        return True

    def set_from_signal(self, *_: object) -> None:
        self._stop = True


if __name__ == "__main__":
    main()
