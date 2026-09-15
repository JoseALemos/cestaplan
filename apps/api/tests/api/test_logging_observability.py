"""Logging estructurado JSON + middleware de observabilidad (request-id, captura de errores)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cestaplan_api.logging_config import JsonFormatter, setup_logging
from cestaplan_api.main import RequestObservabilityMiddleware


def _record(msg: str, **extra: object) -> logging.LogRecord:
    rec = logging.getLogger("t").makeRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_includes_extra_and_core_fields() -> None:
    out = json.loads(JsonFormatter().format(_record("hola", request_id="abc", status=200)))
    assert out["msg"] == "hola"
    assert out["level"] == "INFO"
    assert out["logger"] == "t"
    assert out["request_id"] == "abc"
    assert out["status"] == 200
    assert "ts" in out


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.getLogger("t").makeRecord(
            "t", logging.ERROR, __file__, 1, "fallo", (), __import__("sys").exc_info()
        )
    out = json.loads(JsonFormatter().format(rec))
    assert "boom" in out["exc"]


def test_setup_logging_is_idempotent() -> None:
    setup_logging("INFO")
    setup_logging("INFO")
    assert len(logging.getLogger().handlers) == 1


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestObservabilityMiddleware)

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict[str, bool]:
        raise RuntimeError("kaboom")

    return app


def test_middleware_sets_request_id_header() -> None:
    resp = TestClient(_app()).get("/ok")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")


def test_middleware_logs_unhandled_exception(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="cestaplan.request"):
        resp = client.get("/boom")
    assert resp.status_code == 500
    assert any("unhandled exception" in r.message for r in caplog.records)
