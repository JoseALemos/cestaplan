"""Fixtures raíz: garantizar una base de datos LOCAL prístina al inicio de la sesión.

El suite corre contra un Postgres local persistente. Cada test es transaccional (rollback en el
teardown), pero un puñado de tests de ingesta/remediación tienen que COMMITear para ejercitar la
persistencia real y limpian lo suyo al terminar. Si una de esas ejecuciones se INTERRUMPE (se mata
el proceso), pytest nunca ejecuta su teardown y deja filas committeadas huérfanas; esas huérfanas
envenenan ejecuciones posteriores — rompen el borrado del catálogo demo (un retailer sintético que
queda referenciado por un external_product) y las asserts de catálogo/admin que cuentan filas
sintéticas.

Para que el suite se auto-cure, hacemos TRUNCATE de todas las tablas de datos una vez, antes de
cualquier test, de modo que cada sesión arranca desde el esquema migrado prístino; el seed de sesión
existente (tests/api) repuebla luego el catálogo demo como siempre. Ninguna migración siembra datos
de referencia duraderos (la única con INSERT es una transformación sobre datos preexistentes, no-op
en BD vacía), así que no se pierde nada de lo que dependan los tests.

BLINDADO a bases de datos LOCALES: si ``DATABASE_URL`` no apunta a localhost, la fixture es un no-op,
de modo que NUNCA puede tocar una base compartida o de producción.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from cestaplan_api.config import get_settings
from cestaplan_api.db import engine

# Hosts considerados locales; cualquier otro (p.ej. el host interno de Railway) desactiva el wipe.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _database_is_local() -> bool:
    raw = get_settings().database_url
    # Quitar el marcador de driver para que urlparse vea un esquema estándar.
    normalized = raw.replace("postgresql+psycopg://", "postgresql://", 1)
    host = urlparse(normalized).hostname or ""
    return host in _LOCAL_HOSTS


@pytest.fixture(scope="session", autouse=True)
def _pristine_local_db() -> None:
    """Vacía todas las tablas de datos una vez al inicio de la sesión (solo BD local)."""
    if not _database_is_local():
        return
    with engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
            .scalars()
            .all()
        )
        if tables:
            joined = ", ".join(f'"{name}"' for name in tables)
            conn.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
