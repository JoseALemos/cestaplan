"""Backup diario de la BD a un volumen: ``pg_dump`` comprimido + retención.

Pensado para correr como servicio cron de Railway (ver ``infra/railway/db-backup.json``) con un
volumen montado en ``BACKUP_DIR``. Defensa en profundidad ADEMÁS de los backups gestionados de
Railway. No sube nada a terceros (sin credenciales externas); un destino off-site (S3/Backblaze)
se añadiría fijando esas credenciales aparte.

Config por entorno:
  BACKUP_DIR              destino de los volcados (por defecto ``/backups``; debe ser el volumen).
  BACKUP_RETENTION_DAYS   días a conservar (por defecto 14).
  DATABASE_URL            cadena de conexión (se normaliza a libpq para ``pg_dump``).
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("cestaplan.backup")

_DUMP_PREFIX = "cestaplan-"
_DUMP_SUFFIX = ".sql.gz"


def _libpq_url(url: str) -> str:
    """Normaliza la URL de SQLAlchemy a la forma que entiende ``pg_dump`` (libpq)."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


def _prune(backup_dir: Path, retention_days: int, now: datetime) -> int:
    """Borra volcados más antiguos que la retención. Devuelve cuántos borró."""
    cutoff = now - timedelta(days=retention_days)
    removed = 0
    for f in backup_dir.glob(f"{_DUMP_PREFIX}*{_DUMP_SUFFIX}"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC)
        if mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def run() -> Path:
    """Ejecuta un backup y aplica retención. Devuelve la ruta del volcado creado."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL no está definida; no se puede hacer backup")
    backup_dir = Path(os.environ.get("BACKUP_DIR", "/backups"))
    retention_days = int(os.environ.get("BACKUP_RETENTION_DAYS", "14"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Un contenedor cron es efímero: si BACKUP_DIR no es un volumen montado, el volcado se escribe
    # y DESAPARECE al terminar el contenedor (backup/DR ilusorio). No abortamos (Railway ofrece
    # backups gestionados de Postgres como fuente primaria), pero avisamos ruidosamente salvo que
    # el operador lo reconozca explícitamente con BACKUP_DIR_ACK_EPHEMERAL=true.
    if (
        not os.path.ismount(backup_dir)
        and os.environ.get("BACKUP_DIR_ACK_EPHEMERAL") != "true"
    ):
        logger.warning(
            "BACKUP_DIR %s no parece un volumen montado (posible filesystem EFÍMERO): los volcados "
            "se perderían al reiniciar el contenedor. Monta un volumen persistente en BACKUP_DIR o "
            "define BACKUP_DIR_ACK_EPHEMERAL=true si es intencionado.",
            backup_dir,
        )

    now = datetime.now(UTC)
    target = backup_dir / f"{_DUMP_PREFIX}{now:%Y%m%d-%H%M%S}{_DUMP_SUFFIX}"
    tmp = target.with_suffix(".sql.gz.partial")

    # pg_dump a stdout -> gzip a un fichero temporal -> rename atómico al final (nunca deja un
    # volcado a medias con nombre definitivo si el proceso muere).
    # Argumentos fijos, sin shell (no hay inyección posible).
    proc = subprocess.Popen(
        ["pg_dump", "--no-owner", "--no-privileges", "--format=plain", _libpq_url(database_url)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    with gzip.open(tmp, "wb") as gz:
        shutil.copyfileobj(proc.stdout, gz)
    code = proc.wait()
    if code != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump falló con código {code}")
    tmp.rename(target)
    size_mb = target.stat().st_size / (1024 * 1024)
    removed = _prune(backup_dir, retention_days, now)
    logger.info(
        "backup OK: %s (%.1f MB); retención %d días, %d antiguos borrados",
        target.name,
        size_mb,
        retention_days,
        removed,
    )
    return target


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        run()
    except Exception:
        logger.exception("backup FALLÓ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
