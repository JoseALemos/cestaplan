"""Tests de las funciones puras del backup (sin ejecutar pg_dump real)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cestaplan_api.jobs.backup_database import _libpq_url, _prune


def test_libpq_url_strips_sqlalchemy_driver() -> None:
    assert _libpq_url("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert _libpq_url("postgresql+psycopg2://u:p@h/db") == "postgresql://u:p@h/db"
    assert _libpq_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"
    assert _libpq_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_prune_removes_only_expired(tmp_path: Path) -> None:
    now = datetime(2026, 9, 13, 2, 0, tzinfo=UTC)
    old = tmp_path / "cestaplan-20260101-020000.sql.gz"
    fresh = tmp_path / "cestaplan-20260912-020000.sql.gz"
    unrelated = tmp_path / "keepme.txt"
    for f in (old, fresh, unrelated):
        f.write_bytes(b"x")
    os.utime(old, (old.stat().st_atime, (now - timedelta(days=30)).timestamp()))
    os.utime(fresh, (fresh.stat().st_atime, (now - timedelta(days=1)).timestamp()))

    removed = _prune(tmp_path, retention_days=14, now=now)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()  # solo toca los volcados con el prefijo/sufijo del backup
