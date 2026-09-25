"""SEC10: purga de retención del registro de auditoría."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from cestaplan_api.jobs.purge_audit_logs import run
from cestaplan_api.models import AuditLog


def test_purge_dry_run_counts_but_keeps(db_session: Session) -> None:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    old = AuditLog(action="viejo", occurred_at=now - timedelta(days=400))
    recent = AuditLog(action="reciente", occurred_at=now - timedelta(days=10))
    db_session.add_all([old, recent])
    db_session.flush()
    old_id, recent_id = old.id, recent.id

    n = run(db_session, retention_days=365, commit=False, now=now)
    assert n == 1
    assert db_session.get(AuditLog, old_id) is not None  # dry-run no borra
    assert db_session.get(AuditLog, recent_id) is not None


def test_purge_commit_deletes_old_keeps_recent(db_session: Session) -> None:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    old = AuditLog(action="viejo", occurred_at=now - timedelta(days=400))
    recent = AuditLog(action="reciente", occurred_at=now - timedelta(days=10))
    db_session.add_all([old, recent])
    db_session.flush()
    old_id, recent_id = old.id, recent.id

    run(db_session, retention_days=365, commit=True, now=now)
    assert db_session.get(AuditLog, old_id) is None       # anterior a la retención -> borrado
    assert db_session.get(AuditLog, recent_id) is not None  # dentro de la retención -> conservado
