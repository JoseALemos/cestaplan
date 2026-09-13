"""Cabeceras de seguridad en todas las respuestas + allowlist de Host (TrustedHost)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cestaplan_api.config import Settings
from cestaplan_api.main import app, configure_middleware


def test_security_headers_present_on_responses() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert (
        resp.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"
    )
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert (
        resp.headers["content-security-policy"]
        == "default-src 'none'; frame-ancestors 'none'"
    )


def test_trusted_host_allowlist_blocks_unknown_host() -> None:
    sub_app = FastAPI()

    @sub_app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    configure_middleware(sub_app, Settings(trusted_hosts="allowed.example"))
    client = TestClient(sub_app)

    ok = client.get("/ping", headers={"host": "allowed.example"})
    assert ok.status_code == 200
    # Las cabeceras de seguridad también viajan en las respuestas de este app.
    assert ok.headers["x-frame-options"] == "DENY"

    bad = client.get("/ping", headers={"host": "evil.example"})
    assert bad.status_code == 400
