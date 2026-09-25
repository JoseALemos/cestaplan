"""Purga de retención del registro de auditoría (SEC10 — minimización RGPD).

El ``audit_log`` acumula eventos (algunos con email en metadatos, p.ej. intentos de login), que no
deben conservarse más de lo necesario. Este job borra las filas más antiguas que la retención
configurada. Dry-run por defecto; ``--commit`` persiste.

    python -m cestaplan_api.jobs.purge_audit_logs             # dry-run (cuántas se borrarían)
    python -m cestaplan_api.jobs.purge_audit_logs --commit    # borra

Retención: ``AUDIT_RETENTION_DAYS`` (por defecto 365). CestaPlan es un planificador de comidas sin
obligación legal de conservación prolongada de estos logs; el responsable debe confirmar el periodo
según su política. Pensado para un cron (opt-in, como db-backup).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import AuditLog

logger = logging.getLogger("cestaplan.audit_purge")

_DEFAULT_RETENTION_DAYS = 365


def _retention_days() -> int:
    try:
        return max(1, int(os.environ.get("AUDIT_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


def run(session: Session, *, retention_days: int, commit: bool, now: datetime | None = None) -> int:
    """Borra los AuditLog anteriores a ``retention_days``. Devuelve cuántos (a borrar/borrados)."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    count = session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.occurred_at < cutoff)
    ).scalar_one()
    if commit and count:
        session.execute(delete(AuditLog).where(AuditLog.occurred_at < cutoff))
        session.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Borrar (por defecto dry-run).")
    parser.add_argument("--retention-days", type=int, default=_retention_days())
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        count = run(session, retention_days=args.retention_days, commit=args.commit)
    finally:
        session.close()
    verb = "borradas" if args.commit else "a borrar (dry-run)"
    logger.info(
        "audit purge: %d filas %s (retención %d días)", count, verb, args.retention_days
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
