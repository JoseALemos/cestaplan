"""Admin HTTP API tests: authz, upload/preview, commit, rollback, source status."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from .conftest import csrf, login, promote_to_admin, register

_HEADER = (
    "retailer_slug,store_external_code,store_postal_code,product_external_id,product_name,"
    "brand,category,barcode,package_quantity,package_unit,amount,currency,unit_price,"
    "promotion,availability,source_type,source_name,source_url,observed_at,expires_at,"
    "confidence_score,verification_status"
)
_ROW = (
    "acme,ACME-1,28013,ACME-CHK-500,Pollo 500 g,MarcaX,carnes,8400000000017,500,g,3.49,"
    "EUR,6.98,,in_stock,admin_import,Cat operador,,2026-07-20T08:00:00Z,"
    "2026-08-20T08:00:00Z,0.9,unverified"
)
_CSV = f"{_HEADER}\n{_ROW}\n"


def _email() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}@example.com"


def _upload(client: TestClient, token: str, dry_run: str = "true") -> dict:
    return client.post(
        "/api/v1/admin/imports",
        files={"file": ("sample.csv", _CSV, "text/csv")},
        data={"dry_run": dry_run},
        headers=csrf(token),
    ).json()


def test_non_admin_forbidden(client: TestClient) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    resp = client.post(
        "/api/v1/admin/imports",
        files={"file": ("sample.csv", _CSV, "text/csv")},
        data={"dry_run": "true"},
        headers=csrf(token),
    )
    assert resp.status_code == 403


def test_make_admin_flips_access(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    assert (
        client.post(
            "/api/v1/admin/imports",
            files={"file": ("s.csv", _CSV, "text/csv")},
            data={"dry_run": "true"},
            headers=csrf(token),
        ).status_code
        == 403
    )
    promote_to_admin(db_session, email)
    resp = client.post(
        "/api/v1/admin/imports",
        files={"file": ("s.csv", _CSV, "text/csv")},
        data={"dry_run": "true"},
        headers=csrf(token),
    )
    assert resp.status_code == 201, resp.text


def test_dry_run_preview_then_commit_then_rollback(
    client: TestClient, db_session: Session
) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)

    # Dry run preview.
    preview = _upload(client, token, dry_run="true")
    assert preview["status"] == "dry_run"
    assert preview["counts"]["created"] == 1
    assert preview["counts"]["error_count"] == 0
    assert preview["would_change"][0]["action"] == "created"

    # Real (pending) import + commit.
    created = _upload(client, token, dry_run="false")
    import_id = created["id"]
    assert created["status"] == "pending"

    commit = client.post(
        f"/api/v1/admin/imports/{import_id}/commit", headers=csrf(token)
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "committed"

    # Detail + list reflect it.
    detail = client.get(f"/api/v1/admin/imports/{import_id}").json()
    assert detail["counts"]["created"] == 1
    listing = client.get("/api/v1/admin/imports").json()
    assert any(item["id"] == import_id for item in listing)

    # Rollback.
    rolled = client.post(
        f"/api/v1/admin/imports/{import_id}/rollback", headers=csrf(token)
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "rolled_back"
    assert rolled.json()["deleted_prices"] == 1


def test_commit_requires_csrf(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    created = _upload(client, token, dry_run="false")
    # No CSRF header -> 403.
    resp = client.post(f"/api/v1/admin/imports/{created['id']}/commit")
    assert resp.status_code == 403


def test_double_commit_conflicts(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    created = _upload(client, token, dry_run="false")
    import_id = created["id"]
    assert (
        client.post(
            f"/api/v1/admin/imports/{import_id}/commit", headers=csrf(token)
        ).status_code
        == 200
    )
    conflict = client.post(
        f"/api/v1/admin/imports/{import_id}/commit", headers=csrf(token)
    )
    assert conflict.status_code == 409


def test_sources_lists_adapters(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    login(client, email)
    promote_to_admin(db_session, email)

    resp = client.get("/api/v1/admin/sources")
    assert resp.status_code == 200
    by_key = {row["adapter_key"]: row for row in resp.json()}
    assert by_key["csv"]["enabled"] is True
    assert by_key["mercadona_community"]["enabled"] is False
    assert by_key["mercadona_community"]["is_community"] is True
    assert by_key["aldi"]["status"] == "skeleton"


def test_bad_format_rejected(client: TestClient, db_session: Session) -> None:
    email = _email()
    register(client, email)
    token = login(client, email)
    promote_to_admin(db_session, email)
    resp = client.post(
        "/api/v1/admin/imports",
        files={"file": ("data.txt", b"whatever", "text/plain")},
        data={"dry_run": "true"},
        headers=csrf(token),
    )
    assert resp.status_code == 400
