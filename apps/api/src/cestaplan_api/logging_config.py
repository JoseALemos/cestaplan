"""Logging estructurado (JSON a stdout) para self-host.

Sin dependencias externas: sale por stdout, que Railway (y cualquier operador self-host) recoge y
puede filtrar/buscar. Cada línea es un objeto JSON con ``ts/level/logger/msg`` más los campos
``extra`` que pase el emisor (p. ej. ``request_id``, ``path``, ``status``, ``duration_ms``). Las
excepciones incluyen el traceback en ``exc``. Es el baseline de observabilidad de la fase
self-host; la fase cloud comercial añadiría un colector (Sentry/GlitchTip) por encima.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Atributos estándar de LogRecord (no son campos "extra" del emisor).
_RESERVED: frozenset[str] = frozenset(
    logging.makeLogRecord({}).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Formatea cada registro como una línea JSON, incluyendo los campos ``extra``."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configura el logger raíz para emitir JSON a stdout. Idempotente."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


__all__ = ["JsonFormatter", "setup_logging"]
