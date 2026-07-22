"""API tests for the ingestion admin router (spec §18, FASE B).

Covers admin enforcement (403 for non-admins), CSRF on mutations, the connector
list/detail + enable/disable flow (including the 409 for a ``permission_required``
connector), manual crawl create → list → detail → cancel → retry, anomaly approve/reject
moving status without fabricating a price, and an honest coverage view. Everything runs
inside the shared transactional ``db_session`` and is rolled back on teardown.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import get_db
from cestaplan_api.ingestion import (
    AnomalyStatus,
    ConnectorStatus,
    JobStatus,
    LegalStatus,
    RunStatus,
    RunType,
    Severity,
)
from cestaplan_api.models import (
    ConnectorState,
    CoverageSnapshot,
    CrawlJob,
    CrawlRun,
    DataSource,
    PriceAnomaly,
    Retailer,
    Store,
    User,
)
from cestaplan_api.routers import auth as auth_router
from cestaplan_api.routers import ingestion_admin

from .conftest import csrf, login, register


# --------------------------------------------------------------------------- #
# App / fixtures
# --------------------------------------------------------------------------- #
def _client(db_session: Session) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(ingestion_admin.router)

    def _override_get_db() -> Iterator[Session]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


def _email() -> str:
    return f"ingadmin-{uuid.uuid4().hex[:12]}@example.com"


def _make_admin(client: TestClient, db_session: Session) -> str:
    """Register a user, promote to admin, log in and return the CSRF token."""
    email = _email()
    register(client, email)
    user = db_session.execute(
        select(User).where(User.email == email)
    ).scalar_one()
    user.is_admin = True
    db_session.flush()
    return login(client, email)


def _make_user(client: TestClient) -> str:
    email = _email()
    register(client, email)
    return login(client, email)


def _make_retailer(
    db: Session, *, adapter_key: str = "demo", slug_prefix: str = "conn"
) -> Retailer:
    retailer = Retailer(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
        name=f"{slug_prefix.title()} {uuid.uuid4().hex[:4]}",
        adapter_key=adapter_key,
        country="ES",
        is_active=True,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    return retailer


def _make_data_source(
    db: Session, adapter_key: str, legal_status: LegalStatus
) -> DataSource:
    ds = DataSource(
        slug=f"src-{uuid.uuid4().hex[:8]}",
        name=f"Source {uuid.uuid4().hex[:4]}",
        source_type="community_connector",
        adapter_key=adapter_key,
        is_enabled=False,
        legal_status=legal_status.value,
    )
    db.add(ds)
    db.flush()
    return ds


def _make_state(
    db: Session, retailer: Retailer, status_value: ConnectorStatus
) -> ConnectorState:
    state = ConnectorState(
        retailer_id=retailer.id,
        store_id=None,
        connector_version="1.0.0",
        parser_version="1.0.0",
        status=status_value.value,
        consecutive_failures=0,
    )
    db.add(state)
    db.flush()
    return state


def _make_store(db: Session, retailer: Retailer) -> Store:
    store = Store(
        retailer_id=retailer.id,
        external_code=f"code-{uuid.uuid4().hex[:8]}",
        name="Tienda",
        locality="Madrid",
        postal_code="28001",
        is_active=True,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


# --------------------------------------------------------------------------- #
# Auth / CSRF
# --------------------------------------------------------------------------- #
def test_connectors_requires_admin(db_session: Session) -> None:
    client = _client(db_session)
    _make_user(client)  # non-admin session cookie is now set on the client
    resp = client.get("/api/v1/admin/connectors")
    assert resp.status_code == 403, resp.text


def test_connectors_requires_authentication(db_session: Session) -> None:
    client = _client(db_session)
    resp = client.get("/api/v1/admin/connectors")
    assert resp.status_code == 401, resp.text


def test_mutation_requires_csrf(db_session: Session) -> None:
    client = _client(db_session)
    _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    # No CSRF header -> 403 even for an admin.
    resp = client.post(f"/api/v1/admin/connectors/{retailer.slug}/disable")
    assert resp.status_code == 403, resp.text


# --------------------------------------------------------------------------- #
# Connectors
# --------------------------------------------------------------------------- #
def test_list_and_detail_connector(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    # A real registry adapter (capabilities present) that the demo seed does NOT back
    # with a DataSource, so the connector's legal footing is unambiguously ours.
    retailer = _make_retailer(db_session, adapter_key="lidl")
    _make_data_source(db_session, "lidl", LegalStatus.PUBLIC)
    _make_state(db_session, retailer, ConnectorStatus.ACTIVE)

    resp = client.get("/api/v1/admin/connectors", headers=csrf(token))
    assert resp.status_code == 200, resp.text
    codes = {c["code"]: c for c in resp.json()}
    assert retailer.slug in codes
    summary = codes[retailer.slug]
    assert summary["status"] == ConnectorStatus.ACTIVE.value
    assert summary["legal_status"] == LegalStatus.PUBLIC.value

    detail = client.get(f"/api/v1/admin/connectors/{retailer.slug}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["code"] == retailer.slug
    assert payload["capabilities"] is not None
    assert payload["data_source"] is not None


def test_connector_detail_unknown_code_404(db_session: Session) -> None:
    client = _client(db_session)
    _make_admin(client, db_session)
    resp = client.get("/api/v1/admin/connectors/does-not-exist")
    assert resp.status_code == 404, resp.text


def test_enable_then_disable_connector(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session, adapter_key="demo")
    _make_data_source(db_session, "demo", LegalStatus.PUBLIC)
    _make_state(db_session, retailer, ConnectorStatus.DISABLED)

    enable = client.post(
        f"/api/v1/admin/connectors/{retailer.slug}/enable", headers=csrf(token)
    )
    assert enable.status_code == 200, enable.text
    assert enable.json()["status"] == ConnectorStatus.ACTIVE.value
    assert enable.json()["changed"] is True

    state = db_session.execute(
        select(ConnectorState).where(ConnectorState.retailer_id == retailer.id)
    ).scalar_one()
    assert state.status == ConnectorStatus.ACTIVE.value

    disable = client.post(
        f"/api/v1/admin/connectors/{retailer.slug}/disable", headers=csrf(token)
    )
    assert disable.status_code == 200, disable.text
    assert disable.json()["status"] == ConnectorStatus.DISABLED.value


def test_enable_permission_required_connector_409(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session, adapter_key="mercadona_community")
    _make_data_source(
        db_session, "mercadona_community", LegalStatus.PERMISSION_REQUIRED
    )
    _make_state(db_session, retailer, ConnectorStatus.DISABLED)

    resp = client.post(
        f"/api/v1/admin/connectors/{retailer.slug}/enable", headers=csrf(token)
    )
    # Refused because the source's legal footing requires permission; the 409 itself is
    # proof it was not activated (the request rolls back, leaving the connector disabled).
    assert resp.status_code == 409, resp.text
    assert "permiso" in resp.json()["detail"].lower()


def test_enable_unsupported_connector_409(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session, adapter_key="demo")
    _make_data_source(db_session, "demo", LegalStatus.PUBLIC)
    _make_state(db_session, retailer, ConnectorStatus.UNSUPPORTED)

    resp = client.post(
        f"/api/v1/admin/connectors/{retailer.slug}/enable", headers=csrf(token)
    )
    assert resp.status_code == 409, resp.text


def test_connector_health_check(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session, adapter_key="demo")
    _make_state(db_session, retailer, ConnectorStatus.ACTIVE)

    resp = client.post(
        f"/api/v1/admin/connectors/{retailer.slug}/health-check", headers=csrf(token)
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["code"] == retailer.slug
    assert payload["status"] == ConnectorStatus.ACTIVE.value
    assert "checked_at" in payload


# --------------------------------------------------------------------------- #
# Crawls
# --------------------------------------------------------------------------- #
def test_create_crawl_appears_in_list_and_detail(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    store = _make_store(db_session, retailer)

    create = client.post(
        "/api/v1/admin/crawls",
        json={
            "retailer_code": retailer.slug,
            "run_type": RunType.PRICES.value,
            "store_id": str(store.public_id),
        },
        headers=csrf(token),
    )
    assert create.status_code == 202, create.text
    crawl_id = create.json()["id"]
    assert create.json()["run_type"] == RunType.PRICES.value
    assert create.json()["jobs_total"] == 1

    listed = client.get(
        "/api/v1/admin/crawls", params={"retailer_code": retailer.slug}
    )
    assert listed.status_code == 200, listed.text
    assert crawl_id in {row["id"] for row in listed.json()}

    detail = client.get(f"/api/v1/admin/crawls/{crawl_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == crawl_id
    assert detail.json()["counters"]["accepted"] == 0

    # A job was actually enqueued for the run.
    run = db_session.execute(
        select(CrawlRun).where(CrawlRun.public_id == uuid.UUID(crawl_id))
    ).scalar_one()
    jobs = db_session.execute(
        select(CrawlJob).where(CrawlJob.crawl_run_id == run.id)
    ).scalars().all()
    assert len(jobs) == 1


def test_create_crawl_store_idor(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer_a = _make_retailer(db_session)
    retailer_b = _make_retailer(db_session)
    store_b = _make_store(db_session, retailer_b)

    resp = client.post(
        "/api/v1/admin/crawls",
        json={
            "retailer_code": retailer_a.slug,
            "run_type": RunType.PRICES.value,
            "store_id": str(store_b.public_id),
        },
        headers=csrf(token),
    )
    assert resp.status_code == 404, resp.text


def test_cancel_crawl(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)

    create = client.post(
        "/api/v1/admin/crawls",
        json={"retailer_code": retailer.slug, "run_type": RunType.DISCOVERY.value},
        headers=csrf(token),
    )
    crawl_id = create.json()["id"]

    cancel = client.post(
        f"/api/v1/admin/crawls/{crawl_id}/cancel", headers=csrf(token)
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == RunStatus.CANCELLED.value

    # Cancelling again is a conflict (terminal state).
    again = client.post(
        f"/api/v1/admin/crawls/{crawl_id}/cancel", headers=csrf(token)
    )
    assert again.status_code == 409, again.text

    # The pending job was cancelled too.
    run = db_session.execute(
        select(CrawlRun).where(CrawlRun.public_id == uuid.UUID(crawl_id))
    ).scalar_one()
    jobs = db_session.execute(
        select(CrawlJob).where(CrawlJob.crawl_run_id == run.id)
    ).scalars().all()
    assert all(job.status == JobStatus.CANCELLED.value for job in jobs)


def test_retry_crawl_requeues_failed_jobs(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)

    create = client.post(
        "/api/v1/admin/crawls",
        json={"retailer_code": retailer.slug, "run_type": RunType.PRICES.value},
        headers=csrf(token),
    )
    crawl_id = create.json()["id"]
    run = db_session.execute(
        select(CrawlRun).where(CrawlRun.public_id == uuid.UUID(crawl_id))
    ).scalar_one()

    # Drive the job to dead_letter and the run to failed.
    job = db_session.execute(
        select(CrawlJob).where(CrawlJob.crawl_run_id == run.id)
    ).scalar_one()
    job.status = JobStatus.DEAD_LETTER.value
    job.attempts = 3
    run.status = RunStatus.FAILED.value
    db_session.flush()

    retry = client.post(
        f"/api/v1/admin/crawls/{crawl_id}/retry", headers=csrf(token)
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["requeued_jobs"] == 1
    assert retry.json()["status"] == RunStatus.QUEUED.value

    db_session.refresh(job)
    assert job.status == JobStatus.QUEUED.value
    assert job.attempts == 0


# --------------------------------------------------------------------------- #
# Anomalies
# --------------------------------------------------------------------------- #
def _make_anomaly(
    db: Session, *, status_value: AnomalyStatus = AnomalyStatus.QUARANTINED
) -> PriceAnomaly:
    anomaly = PriceAnomaly(
        anomaly_type="price_spike",
        severity=Severity.HIGH.value,
        expected_value=Decimal("1.2000"),
        actual_value=Decimal("120.0000"),
        details={"ratio": "100"},
        status=status_value.value,
    )
    db.add(anomaly)
    db.flush()
    return anomaly


def test_list_and_filter_anomalies(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    anomaly = _make_anomaly(db_session)

    resp = client.get(
        "/api/v1/admin/anomalies",
        params={"status": AnomalyStatus.QUARANTINED.value},
    )
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert str(anomaly.public_id) in ids
    row = next(r for r in resp.json() if r["id"] == str(anomaly.public_id))
    # Money-ish fields are strings, never floats.
    assert row["expected_value"] == "1.2000"
    assert row["actual_value"] == "120.0000"

    _ = token


def test_approve_anomaly_clears_quarantine_without_touching_prices(
    db_session: Session,
) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    anomaly = _make_anomaly(db_session)

    resp = client.post(
        f"/api/v1/admin/anomalies/{anomaly.public_id}/approve", headers=csrf(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["anomaly"]["status"] == AnomalyStatus.APPROVED.value

    db_session.refresh(anomaly)
    assert anomaly.status == AnomalyStatus.APPROVED.value
    assert anomaly.reviewed_at is not None
    # Approving does not fabricate/alter prices: the anomaly still points at no observation
    # and its numeric fields are unchanged.
    assert anomaly.price_observation_id is None
    assert anomaly.expected_value == Decimal("1.2000")
    assert anomaly.actual_value == Decimal("120.0000")


def test_reject_anomaly(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    anomaly = _make_anomaly(db_session)

    resp = client.post(
        f"/api/v1/admin/anomalies/{anomaly.public_id}/reject", headers=csrf(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["anomaly"]["status"] == AnomalyStatus.REJECTED.value


def test_review_already_reviewed_anomaly_409(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    anomaly = _make_anomaly(db_session, status_value=AnomalyStatus.APPROVED)

    resp = client.post(
        f"/api/v1/admin/anomalies/{anomaly.public_id}/reject", headers=csrf(token)
    )
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# Coverage & sources
# --------------------------------------------------------------------------- #
def test_coverage_reports_honest_status(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    retailer = _make_retailer(db_session)
    snapshot = CoverageSnapshot(
        retailer_id=retailer.id,
        store_id=None,
        observed_at=datetime.now(UTC) - timedelta(hours=1),
        expected_products=100,
        discovered_products=100,
        priced_products=40,
        fresh_prices=40,
        stale_prices=0,
        estimated_prices=0,
        unavailable_products=0,
        coverage_ratio=Decimal("0.4000"),
        weighted_coverage_ratio=Decimal("1.0000"),
        status="insufficient",
    )
    db_session.add(snapshot)
    db_session.flush()

    resp = client.get("/api/v1/admin/coverage", headers=csrf(token))
    assert resp.status_code == 200, resp.text
    rows = {row["retailer_code"]: row for row in resp.json()}
    assert retailer.slug in rows
    row = rows[retailer.slug]
    assert row["status"] == "insufficient"
    assert row["coverage_ratio"] == "0.4000"
    assert row["priced_products"] == 40


def test_sources_lists_legal_status(db_session: Session) -> None:
    client = _client(db_session)
    token = _make_admin(client, db_session)
    ds = _make_data_source(db_session, "demo", LegalStatus.AUTHORIZED)

    resp = client.get("/api/v1/admin/sources", headers=csrf(token))
    assert resp.status_code == 200, resp.text
    by_slug = {row["slug"]: row for row in resp.json()}
    assert ds.slug in by_slug
    assert by_slug[ds.slug]["legal_status"] == LegalStatus.AUTHORIZED.value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
