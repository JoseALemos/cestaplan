"""Admin API for the price-ingestion subsystem (spec §18, FASE B).

Prefix ``/api/v1/admin``. Every route requires a platform admin
(:func:`cestaplan_api.deps.require_admin`); every mutation also requires CSRF. This router
is the operator console over the FASE A foundation — connectors, crawl runs, anomalies and
coverage — and it is deliberately *honest and safe*:

- Entities are addressed by a public ``code`` (a connector) or public UUID (crawl/anomaly);
  internal integer PKs are never accepted or returned, and a store is checked to belong to
  its retailer (no IDOR).
- Money/ratios are strings, counts are integers, and no raw HTML capture body, response
  header, token or source secret is ever exposed.
- Enabling a connector only flips ``disabled ↔ active`` and refuses (409) any connector whose
  legal footing does not permit it (``permission_required`` / ``prohibited``) or whose state
  is structurally ``unsupported``.
- Approving an anomaly *clears quarantine only* — it never fabricates or mutates a price.

Import endpoints (``/imports``, ``/sources/*/sync`` …) live in :mod:`routers.admin`; this
router does not duplicate them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.registry import get_adapter
from cestaplan_api.deps import AdminUser, DbSession, verify_csrf
from cestaplan_api.ingestion import (
    AnomalyStatus,
    ConnectorStatus,
    JobStatus,
    LegalStatus,
    PriceScope,
    PriceType,
    RunStatus,
)
from cestaplan_api.ingestion.audit import SourceAuditService
from cestaplan_api.ingestion.coverage import PriceCoverageService
from cestaplan_api.ingestion.health import ConnectorHealthService
from cestaplan_api.ingestion.manual_entry import ManualPriceError, record_manual_price
from cestaplan_api.ingestion.queue import cancel_job
from cestaplan_api.ingestion.run_service import CrawlRunService, JobSpec
from cestaplan_api.models import (
    ConnectorState,
    CoverageSnapshot,
    CrawlJob,
    CrawlRun,
    DataSource,
    PriceAnomaly,
    PriceObservation,
    Product,
    Retailer,
    Store,
)
from cestaplan_api.schemas.ingestion import (
    AnomalyReviewResponse,
    AnomalySummary,
    ConnectorActionResponse,
    ConnectorCapabilities,
    ConnectorDetail,
    ConnectorHealthReport,
    ConnectorSummary,
    CoverageRow,
    CrawlCounters,
    CrawlCreateRequest,
    CrawlDetail,
    CrawlSummary,
    DataSourceInfo,
    SourceRow,
)

router = APIRouter(prefix="/api/v1/admin", tags=["ingestion-admin"])

# Legal footings under which a connector may NOT be enabled to ``active``.
_LEGAL_BLOCKED = frozenset(
    {LegalStatus.PERMISSION_REQUIRED.value, LegalStatus.PROHIBITED.value}
)
_RUN_TERMINAL = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_JOB_CANCELLABLE = frozenset({JobStatus.QUEUED.value, JobStatus.LOCKED.value})
_JOB_RETRYABLE = frozenset({JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value})
_ANOMALY_REVIEWED = frozenset(
    {AnomalyStatus.APPROVED.value, AnomalyStatus.REJECTED.value}
)


def _now() -> datetime:
    return datetime.now(UTC)


def _dec(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def _get_retailer_by_code(db: Session, code: str) -> Retailer:
    retailer = db.execute(
        select(Retailer).where(Retailer.slug == code)
    ).scalar_one_or_none()
    if retailer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conector no encontrado")
    return retailer


def _data_source_for(db: Session, adapter_key: str | None) -> DataSource | None:
    if not adapter_key:
        return None
    return db.execute(
        select(DataSource)
        .where(DataSource.adapter_key == adapter_key)
        .order_by(DataSource.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def _legal_status_for(ds: DataSource | None) -> str:
    return ds.legal_status if ds is not None else LegalStatus.UNKNOWN.value


def _capabilities_for(retailer: Retailer) -> ConnectorCapabilities | None:
    adapter = get_adapter(retailer.adapter_key)
    if adapter is None:
        return None
    caps = adapter.capabilities()
    return ConnectorCapabilities(
        full_catalog=caps.supports_store_catalog,
        prices=caps.supports_get_price,
        promotions=False,
        availability=caps.supports_get_availability,
        store_catalog=caps.supports_store_catalog,
        requires_network=caps.requires_network,
    )


def _latest_state(
    db: Session, retailer_id: int, *, store_id: int | None = None
) -> ConnectorState | None:
    stmt = (
        select(ConnectorState)
        .where(ConnectorState.retailer_id == retailer_id)
        .order_by(ConnectorState.updated_at.desc(), ConnectorState.id.desc())
        .limit(1)
    )
    if store_id is None:
        stmt = stmt.where(ConnectorState.store_id.is_(None))
    else:
        stmt = stmt.where(ConnectorState.store_id == store_id)
    return db.execute(stmt).scalars().first()


def _get_or_create_retailer_state(
    db: Session, retailer: Retailer
) -> ConnectorState:
    """Retailer-wide (store-less) connector state, created lazily for enable/disable."""
    state = _latest_state(db, retailer.id, store_id=None)
    if state is not None:
        return state
    adapter = get_adapter(retailer.adapter_key)
    version = adapter.metadata().version if adapter is not None else "unknown"
    state = ConnectorState(
        retailer_id=retailer.id,
        store_id=None,
        connector_version=version,
        parser_version=version,
        status=ConnectorStatus.DISABLED.value,
        consecutive_failures=0,
    )
    db.add(state)
    db.flush()
    return state


def _data_source_info(ds: DataSource | None) -> DataSourceInfo | None:
    if ds is None:
        return None
    return DataSourceInfo(
        slug=ds.slug,
        name=ds.name,
        source_type=ds.source_type,
        legal_status=ds.legal_status,
        is_enabled=ds.is_enabled,
        url=ds.url,
    )


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _store_public_id(db: Session, store_id: int | None) -> str | None:
    if store_id is None:
        return None
    store = db.get(Store, store_id)
    return str(store.public_id) if store is not None else None


def _retailer_code(db: Session, retailer_id: int, cache: dict[int, str]) -> str:
    code = cache.get(retailer_id)
    if code is None:
        retailer = db.get(Retailer, retailer_id)
        code = retailer.slug if retailer is not None else str(retailer_id)
        cache[retailer_id] = code
    return code


def _crawl_summary(
    db: Session, run: CrawlRun, cache: dict[int, str]
) -> CrawlSummary:
    return CrawlSummary(
        id=str(run.public_id),
        retailer_code=_retailer_code(db, run.retailer_id, cache),
        store_id=_store_public_id(db, run.store_id),
        run_type=run.run_type,
        status=run.status,
        scheduled_at=run.scheduled_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        coverage_score=_dec(run.coverage_score),
        counters=CrawlCounters(
            discovered=run.discovered_count,
            fetched=run.fetched_count,
            parsed=run.parsed_count,
            accepted=run.accepted_count,
            rejected=run.rejected_count,
            quarantined=run.quarantined_count,
            errors=run.error_count,
        ),
    )


def _crawl_detail(
    db: Session, run: CrawlRun, *, requeued_jobs: int | None = None
) -> CrawlDetail:
    summary = _crawl_summary(db, run, {})
    jobs_total = db.execute(
        select(CrawlJob).where(CrawlJob.crawl_run_id == run.id)
    ).scalars().all()
    return CrawlDetail(
        **summary.model_dump(),
        connector_version=run.connector_version,
        parser_version=run.parser_version,
        jobs_total=len(jobs_total),
        requeued_jobs=requeued_jobs,
    )


def _coverage_row(
    db: Session,
    retailer: Retailer,
    snapshot: CoverageSnapshot,
) -> CoverageRow:
    store_name: str | None = None
    if snapshot.store_id is not None:
        store = db.get(Store, snapshot.store_id)
        store_name = store.name if store is not None else None
    return CoverageRow(
        retailer_code=retailer.slug,
        retailer_name=retailer.name,
        store_id=_store_public_id(db, snapshot.store_id),
        store_name=store_name,
        observed_at=snapshot.observed_at,
        status=snapshot.status,
        expected_products=snapshot.expected_products,
        discovered_products=snapshot.discovered_products,
        priced_products=snapshot.priced_products,
        fresh_prices=snapshot.fresh_prices,
        stale_prices=snapshot.stale_prices,
        estimated_prices=snapshot.estimated_prices,
        unavailable_products=snapshot.unavailable_products,
        coverage_ratio=_dec(snapshot.coverage_ratio),
        weighted_coverage_ratio=_dec(snapshot.weighted_coverage_ratio),
    )


def _anomaly_summary(db: Session, anomaly: PriceAnomaly) -> AnomalySummary:
    run_pid: str | None = None
    if anomaly.crawl_run_id is not None:
        run = db.get(CrawlRun, anomaly.crawl_run_id)
        run_pid = str(run.public_id) if run is not None else None
    obs_pid: str | None = None
    if anomaly.price_observation_id is not None:
        obs = db.get(PriceObservation, anomaly.price_observation_id)
        obs_pid = str(obs.public_id) if obs is not None else None
    return AnomalySummary(
        id=str(anomaly.public_id),
        anomaly_type=anomaly.anomaly_type,
        severity=anomaly.severity,
        status=anomaly.status,
        expected_value=_dec(anomaly.expected_value),
        actual_value=_dec(anomaly.actual_value),
        details=anomaly.details,
        crawl_run_id=run_pid,
        price_observation_id=obs_pid,
        created_at=anomaly.created_at,
        reviewed_at=anomaly.reviewed_at,
    )


# --------------------------------------------------------------------------- #
# Connectors
# --------------------------------------------------------------------------- #
@router.get("/connectors")
def list_connectors(admin: AdminUser, db: DbSession) -> list[ConnectorSummary]:
    """List every retailer's connector with its live state and legal footing."""
    retailers = (
        db.execute(select(Retailer).order_by(Retailer.slug.asc())).scalars().all()
    )
    out: list[ConnectorSummary] = []
    for retailer in retailers:
        state = _latest_state(db, retailer.id)
        ds = _data_source_for(db, retailer.adapter_key)
        out.append(
            ConnectorSummary(
                code=retailer.slug,
                name=retailer.name,
                status=state.status if state is not None else None,
                legal_status=_legal_status_for(ds),
                last_success_at=state.last_success_at if state is not None else None,
                consecutive_failures=(
                    state.consecutive_failures if state is not None else 0
                ),
                circuit_open_until=(
                    state.circuit_open_until if state is not None else None
                ),
                capabilities=_capabilities_for(retailer),
            )
        )
    return out


@router.get("/connectors/{code}")
def get_connector(code: str, admin: AdminUser, db: DbSession) -> ConnectorDetail:
    """Connector detail: state, capabilities, legal footing, latest run and coverage."""
    retailer = _get_retailer_by_code(db, code)
    state = _latest_state(db, retailer.id)
    ds = _data_source_for(db, retailer.adapter_key)

    latest_run = db.execute(
        select(CrawlRun)
        .where(CrawlRun.retailer_id == retailer.id)
        .order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
        .limit(1)
    ).scalars().first()

    snapshot = PriceCoverageService().latest_coverage(db, retailer.id, None)

    return ConnectorDetail(
        code=retailer.slug,
        name=retailer.name,
        status=state.status if state is not None else None,
        legal_status=_legal_status_for(ds),
        last_success_at=state.last_success_at if state is not None else None,
        consecutive_failures=state.consecutive_failures if state is not None else 0,
        circuit_open_until=state.circuit_open_until if state is not None else None,
        capabilities=_capabilities_for(retailer),
        last_attempt_at=state.last_attempt_at if state is not None else None,
        last_error=state.last_error if state is not None else None,
        data_source=_data_source_info(ds),
        latest_run=_crawl_summary(db, latest_run, {}) if latest_run is not None else None,
        coverage=_coverage_row(db, retailer, snapshot) if snapshot is not None else None,
    )


@router.post("/connectors/{code}/enable", dependencies=[Depends(verify_csrf)])
def enable_connector(
    code: str, admin: AdminUser, db: DbSession
) -> ConnectorActionResponse:
    """Move a connector ``disabled → active``; refuse (409) when its footing forbids it."""
    retailer = _get_retailer_by_code(db, code)
    ds = _data_source_for(db, retailer.adapter_key)
    legal = _legal_status_for(ds)
    if legal in _LEGAL_BLOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"No se puede activar un conector con estatus legal '{legal}': "
                "requiere permiso o está prohibido."
            ),
        )

    state = _get_or_create_retailer_state(db, retailer)
    if state.status == ConnectorStatus.UNSUPPORTED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="El conector no es compatible (unsupported) y no puede activarse.",
        )

    if state.status == ConnectorStatus.ACTIVE.value:
        db.flush()
        return ConnectorActionResponse(
            code=retailer.slug,
            status=state.status,
            changed=False,
            detail="El conector ya estaba activo.",
        )

    state.status = ConnectorStatus.ACTIVE.value
    db.flush()
    return ConnectorActionResponse(
        code=retailer.slug,
        status=state.status,
        changed=True,
        detail="Conector activado.",
    )


@router.post("/connectors/{code}/disable", dependencies=[Depends(verify_csrf)])
def disable_connector(
    code: str, admin: AdminUser, db: DbSession
) -> ConnectorActionResponse:
    """Move a connector to ``disabled`` (idempotent)."""
    retailer = _get_retailer_by_code(db, code)
    state = _get_or_create_retailer_state(db, retailer)
    if state.status == ConnectorStatus.DISABLED.value:
        db.flush()
        return ConnectorActionResponse(
            code=retailer.slug,
            status=state.status,
            changed=False,
            detail="El conector ya estaba deshabilitado.",
        )
    state.status = ConnectorStatus.DISABLED.value
    db.flush()
    return ConnectorActionResponse(
        code=retailer.slug,
        status=state.status,
        changed=True,
        detail="Conector deshabilitado.",
    )


@router.post("/connectors/{code}/health-check", dependencies=[Depends(verify_csrf)])
def connector_health_check(
    code: str, admin: AdminUser, db: DbSession
) -> ConnectorHealthReport:
    """Run the connector health service and return the fused report."""
    retailer = _get_retailer_by_code(db, code)
    report = ConnectorHealthService().report(db, retailer.id)
    return ConnectorHealthReport(
        code=retailer.slug,
        checked_at=_now(),
        status=report.status,
        last_attempt_at=report.last_attempt_at,
        last_success_at=report.last_success_at,
        consecutive_failures=report.consecutive_failures,
        circuit_open_until=report.circuit_open_until,
        last_error=report.last_error,
        last_run_status=report.last_run_status,
        last_run_at=report.last_run_at,
        coverage_ratio=_dec(report.coverage_ratio),
        coverage_status=report.coverage_status,
        fresh_prices=report.fresh_prices,
    )


# --------------------------------------------------------------------------- #
# Crawls
# --------------------------------------------------------------------------- #
@router.post(
    "/crawls",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_csrf)],
)
def create_crawl(
    body: CrawlCreateRequest, admin: AdminUser, db: DbSession
) -> CrawlDetail:
    """Create a manual crawl run for a retailer/store and enqueue one job for it."""
    retailer = _get_retailer_by_code(db, body.retailer_code)

    store_id: int | None = None
    if body.store_id is not None:
        store = db.execute(
            select(Store).where(
                Store.public_id == body.store_id,
                Store.retailer_id == retailer.id,
            )
        ).scalar_one_or_none()
        if store is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada"
            )
        store_id = store.id

    service = CrawlRunService(db)
    run = service.create_run(
        retailer_id=retailer.id,
        run_type=body.run_type,
        store_id=store_id,
    )
    spec = JobSpec(
        job_type=body.run_type.value,
        payload={
            "retailer_slug": retailer.slug,
            "store_id": store_id,
            "run_type": body.run_type.value,
            "manual": True,
        },
        idempotency_key=f"manual:{retailer.slug}:{store_id}:{body.run_type.value}:{uuid.uuid4()}",
    )
    service.enqueue_jobs(run, [spec])
    db.flush()
    return _crawl_detail(db, run)


@router.get("/crawls")
def list_crawls(
    admin: AdminUser,
    db: DbSession,
    retailer_code: str | None = Query(default=None),
    crawl_status: str | None = Query(default=None, alias="status"),
    run_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[CrawlSummary]:
    """List crawl runs (newest first), optionally filtered by retailer/status/run type."""
    stmt = select(CrawlRun).order_by(CrawlRun.created_at.desc(), CrawlRun.id.desc())
    if retailer_code is not None:
        retailer = _get_retailer_by_code(db, retailer_code)
        stmt = stmt.where(CrawlRun.retailer_id == retailer.id)
    if crawl_status is not None:
        stmt = stmt.where(CrawlRun.status == crawl_status)
    if run_type is not None:
        stmt = stmt.where(CrawlRun.run_type == run_type)
    runs = db.execute(stmt.limit(limit)).scalars().all()
    cache: dict[int, str] = {}
    return [_crawl_summary(db, run, cache) for run in runs]


def _get_crawl(db: Session, crawl_id: uuid.UUID) -> CrawlRun:
    run = db.execute(
        select(CrawlRun).where(CrawlRun.public_id == crawl_id)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Crawl no encontrado")
    return run


@router.get("/crawls/{crawl_id}")
def get_crawl(crawl_id: uuid.UUID, admin: AdminUser, db: DbSession) -> CrawlDetail:
    """Detail of one crawl run: counters, coverage score and job total."""
    return _crawl_detail(db, _get_crawl(db, crawl_id))


@router.post("/crawls/{crawl_id}/cancel", dependencies=[Depends(verify_csrf)])
def cancel_crawl(
    crawl_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> CrawlDetail:
    """Cancel a non-terminal crawl run and its still-pending jobs."""
    run = _get_crawl(db, crawl_id)
    if run.status in _RUN_TERMINAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"El crawl ya está en estado terminal '{run.status}'.",
        )
    jobs = db.execute(
        select(CrawlJob).where(
            CrawlJob.crawl_run_id == run.id,
            CrawlJob.status.in_(_JOB_CANCELLABLE),
        )
    ).scalars().all()
    for job in jobs:
        cancel_job(db, job)
    CrawlRunService(db).cancel(run)
    db.flush()
    return _crawl_detail(db, run)


@router.post("/crawls/{crawl_id}/retry", dependencies=[Depends(verify_csrf)])
def retry_crawl(
    crawl_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> CrawlDetail:
    """Re-queue the failed / dead-lettered jobs of a run (attempts reset)."""
    run = _get_crawl(db, crawl_id)
    jobs = db.execute(
        select(CrawlJob).where(
            CrawlJob.crawl_run_id == run.id,
            CrawlJob.status.in_(_JOB_RETRYABLE),
        )
    ).scalars().all()
    now = _now()
    for job in jobs:
        job.status = JobStatus.QUEUED.value
        job.attempts = 0
        job.available_at = now
        job.locked_at = None
        job.locked_by = None
        job.heartbeat_at = None
        job.last_error = None
    if jobs and run.status == RunStatus.FAILED.value:
        # Re-open the run so the worker picks the re-queued jobs back up.
        run.status = RunStatus.QUEUED.value
        run.completed_at = None
    db.flush()
    return _crawl_detail(db, run, requeued_jobs=len(jobs))


# --------------------------------------------------------------------------- #
# Anomalies
# --------------------------------------------------------------------------- #
@router.get("/anomalies")
def list_anomalies(
    admin: AdminUser,
    db: DbSession,
    anomaly_status: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AnomalySummary]:
    """List detected anomalies (newest first), filterable by status/severity."""
    stmt = select(PriceAnomaly).order_by(
        PriceAnomaly.created_at.desc(), PriceAnomaly.id.desc()
    )
    if anomaly_status is not None:
        stmt = stmt.where(PriceAnomaly.status == anomaly_status)
    if severity is not None:
        stmt = stmt.where(PriceAnomaly.severity == severity)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [_anomaly_summary(db, anomaly) for anomaly in rows]


def _get_anomaly(db: Session, anomaly_id: uuid.UUID) -> PriceAnomaly:
    anomaly = db.execute(
        select(PriceAnomaly).where(PriceAnomaly.public_id == anomaly_id)
    ).scalar_one_or_none()
    if anomaly is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Anomalía no encontrada")
    return anomaly


def _review_anomaly(
    db: Session, anomaly_id: uuid.UUID, *, new_status: AnomalyStatus, message: str
) -> AnomalyReviewResponse:
    anomaly = _get_anomaly(db, anomaly_id)
    if anomaly.status in _ANOMALY_REVIEWED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"La anomalía ya fue revisada (estado '{anomaly.status}').",
        )
    anomaly.status = new_status.value
    anomaly.reviewed_at = _now()
    db.flush()
    return AnomalyReviewResponse(anomaly=_anomaly_summary(db, anomaly), detail=message)


@router.post("/anomalies/{anomaly_id}/approve", dependencies=[Depends(verify_csrf)])
def approve_anomaly(
    anomaly_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> AnomalyReviewResponse:
    """Approve an anomaly: clears quarantine only — no price is created or altered."""
    return _review_anomaly(
        db,
        anomaly_id,
        new_status=AnomalyStatus.APPROVED,
        message=(
            "Anomalía aprobada: se retira de cuarentena. No se crea ni modifica "
            "ningún precio (los precios sólo cambian por ingesta)."
        ),
    )


@router.post("/anomalies/{anomaly_id}/reject", dependencies=[Depends(verify_csrf)])
def reject_anomaly(
    anomaly_id: uuid.UUID, admin: AdminUser, db: DbSession
) -> AnomalyReviewResponse:
    """Reject an anomaly: marks it rejected (out of quarantine); prices are untouched."""
    return _review_anomaly(
        db,
        anomaly_id,
        new_status=AnomalyStatus.REJECTED,
        message="Anomalía rechazada. No se modifica ningún precio.",
    )


# --------------------------------------------------------------------------- #
# Manual price entry (spec §17)
# --------------------------------------------------------------------------- #
class ManualPriceRequest(BaseModel):
    """An operator-typed price for a product at a retailer (optionally a specific store)."""

    retailer_code: str
    amount: str
    currency: str = "EUR"
    store_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    barcode: str | None = None
    unit: str | None = None
    price_scope: str | None = None
    observed_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)


class ManualPriceResponse(BaseModel):
    """The manual :class:`PriceObservation` that was created (money as strings)."""

    id: str
    retailer_code: str
    store_id: str | None
    product_variant_id: str
    amount: str
    currency: str
    unit_amount: str | None
    unit_code: str | None
    price_scope: str
    price_type: str
    observed_at: datetime
    valid_from: datetime
    confidence_score: str


@router.post(
    "/prices/manual",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_csrf)],
)
def create_manual_price(
    body: ManualPriceRequest, admin: AdminUser, db: DbSession
) -> ManualPriceResponse:
    """Record an operator-typed price as a ``manual`` PriceObservation (append-only, audited).

    Resolves the retailer (by code) and the optional store/product (by public id, checked to
    belong to the retailer — no IDOR), then delegates to
    :func:`~cestaplan_api.ingestion.manual_entry.record_manual_price`. A bad amount/currency/scope
    or a missing target is a 422; the price is never fabricated.
    """
    retailer = _get_retailer_by_code(db, body.retailer_code)

    store: Store | None = None
    if body.store_id is not None:
        store = db.execute(
            select(Store).where(
                Store.public_id == body.store_id,
                Store.retailer_id == retailer.id,
            )
        ).scalar_one_or_none()
        if store is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tienda no encontrada")

    product: Product | None = None
    if body.product_id is not None:
        product = db.execute(
            select(Product).where(
                Product.public_id == body.product_id,
                Product.retailer_id == retailer.id,
            )
        ).scalar_one_or_none()
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")

    scope: PriceScope | None = None
    if body.price_scope is not None:
        try:
            scope = PriceScope(body.price_scope)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Ámbito de precio inválido: {body.price_scope!r}",
            ) from exc

    try:
        obs = record_manual_price(
            db,
            retailer=retailer,
            amount=body.amount,
            store=store,
            product=product,
            barcode=body.barcode,
            currency=body.currency,
            unit=body.unit,
            price_scope=scope,
            price_type=PriceType.MANUAL,
            observed_at=body.observed_at,
            note=body.note,
            user_id=admin.id,
        )
    except ManualPriceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return ManualPriceResponse(
        id=str(obs.public_id),
        retailer_code=retailer.slug,
        store_id=_store_public_id(db, obs.store_id),
        product_variant_id=str(obs.product_variant_id),
        amount=str(obs.amount),
        currency=obs.currency,
        unit_amount=_dec(obs.unit_amount),
        unit_code=obs.unit_code,
        price_scope=obs.price_scope,
        price_type=obs.price_type,
        observed_at=obs.observed_at,
        valid_from=obs.valid_from,
        confidence_score=str(obs.confidence_score),
    )


# --------------------------------------------------------------------------- #
# Coverage & sources
# --------------------------------------------------------------------------- #
@router.get("/coverage")
def list_coverage(admin: AdminUser, db: DbSession) -> list[CoverageRow]:
    """Latest coverage snapshot per retailer (and per store), with an honest status."""
    service = PriceCoverageService()
    retailers = (
        db.execute(
            select(Retailer)
            .where(Retailer.is_active.is_(True))
            .order_by(Retailer.slug.asc())
        )
        .scalars()
        .all()
    )
    rows: list[CoverageRow] = []
    for retailer in retailers:
        wide = service.latest_coverage(db, retailer.id, None)
        if wide is not None:
            rows.append(_coverage_row(db, retailer, wide))
        stores = (
            db.execute(
                select(Store)
                .where(Store.retailer_id == retailer.id, Store.is_active.is_(True))
                .order_by(Store.id.asc())
            )
            .scalars()
            .all()
        )
        for store in stores:
            snapshot = service.latest_coverage(db, retailer.id, store.id)
            if snapshot is not None:
                rows.append(_coverage_row(db, retailer, snapshot))
    return rows


@router.get("/sources")
def list_sources(admin: AdminUser, db: DbSession) -> list[SourceRow]:
    """Every data source with its legal footing and compliance review paper trail."""
    reviews = SourceAuditService().list_sources(db)
    return [
        SourceRow(
            slug=review.slug,
            name=review.name,
            legal_status=review.legal_status,
            terms_reviewed_at=review.terms_reviewed_at,
            robots_reviewed_at=review.robots_reviewed_at,
            notes=review.notes,
        )
        for review in reviews
    ]
