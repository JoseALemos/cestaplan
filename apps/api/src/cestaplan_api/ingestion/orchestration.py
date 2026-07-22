"""End-to-end crawl-job orchestration for the price-ingestion subsystem (FASE B).

This module wires the FASE A pipeline stages into a single runnable vertical for a
:class:`~cestaplan_api.ingestion.contracts.RetailerConnector`:

    resolve store -> discover -> (fetch -> capture RawCapture) -> parse -> normalize ->
    validate -> anomaly-check -> record_observation (append-only) -> update CrawlRun
    counters -> coverage snapshot -> ProductPrice projection

Two safety rules from the spec are enforced here:

- **Never auto-replace last-good.** A per-variant anomaly (e.g. an x100 price spike), a
  batch-level anomaly (catalog collapse / block page) or a failed validation routes the
  affected observation(s) to *quarantine* — stored as closed, disputed rows linked to a
  :class:`PriceAnomaly` — while the previously accepted open row is left untouched.
- **Failure isolation.** :func:`run_crawl_job` converts any connector error into a failed
  :class:`~cestaplan_api.ingestion.crawl_worker.JobOutcome`, so one connector's failure never
  stops the worker loop or another retailer's jobs.

The caller owns the transaction — everything here ``flush``es but never commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import (
    NormalizedObservation,
    RetailerConnector,
    RunStatus,
    Severity,
)
from cestaplan_api.ingestion.anomaly import Anomaly, AnomalyDetector, Batch, PriorStats
from cestaplan_api.ingestion.capture import RawCaptureRepository
from cestaplan_api.ingestion.coverage import PriceCoverageService
from cestaplan_api.ingestion.crawl_worker import JobOutcome
from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.ingestion.http_fetcher import HttpFetchResult
from cestaplan_api.ingestion.price_history import record_observation
from cestaplan_api.ingestion.run_service import CrawlRunService
from cestaplan_api.models import (
    CoverageSnapshot,
    CrawlJob,
    CrawlRun,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
)

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}
_QUARANTINE_SEVERITY = Severity.HIGH


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class PriceSyncResult:
    """Outcome counters of one :func:`run_price_sync` pass (also rolled into the CrawlRun)."""

    discovered: int = 0
    fetched: int = 0
    parsed: int = 0
    accepted: int = 0
    quarantined: int = 0
    rejected: int = 0
    errors: int = 0
    captures: int = 0
    observations: list[PriceObservation] = field(default_factory=list)
    coverage: CoverageSnapshot | None = None
    projected: int = 0


@dataclass(slots=True)
class _PendingObservation:
    """A normalized observation resolved to its variant, awaiting the quarantine decision."""

    obs: NormalizedObservation
    variant: ProductVariant
    raw_capture_id: int | None
    validation_ok: bool


def run_price_sync(
    db: Session,
    retailer: Retailer,
    store: Store | None,
    connector: RetailerConnector,
    *,
    as_of: datetime | None = None,
    crawl_run: CrawlRun | None = None,
    detector: AnomalyDetector | None = None,
) -> PriceSyncResult:
    """Execute the full price-ingestion pipeline for ``connector`` against a retailer/store.

    Resolves the store, discovers products, fetches + captures each one, parses/normalizes to
    observations, validates them, runs batch + per-variant anomaly detection, records every
    observation into the append-only history (quarantining anomalous/invalid ones without
    replacing last-good), rolls the outcome into ``crawl_run``'s counters, then writes a
    coverage snapshot and projects current prices into ``ProductPrice`` for the meal engine.
    """
    as_of = as_of or _now()
    detector = detector or AnomalyDetector(quarantine_severity=_QUARANTINE_SEVERITY)
    result = PriceSyncResult()
    captures = RawCaptureRepository(db)
    service = CrawlRunService(db) if crawl_run is not None else None
    store_id = store.id if store is not None else None
    run_id = crawl_run.id if crawl_run is not None else None

    # -- resolve store (evidence only; the ORM store is the source of truth) ---------- #
    connector.resolve_store(external_store_id=None)

    # -- discover ---------------------------------------------------------------------- #
    discovery = connector.discover_products()
    discovered_ids = _as_id_list(discovery.payload)
    result.discovered = len(discovered_ids)

    # -- fetch -> capture -> parse -> normalize ---------------------------------------- #
    pending: list[_PendingObservation] = []
    any_block_page = False
    for external_id in discovered_ids:
        fetched = connector.fetch_product(external_id)
        capture = captures.store(
            _http_result_from_fetch(fetched, external_id),
            retailer_id=retailer.id,
            source_url=fetched.url or f"demo://{retailer.slug}/{external_id}",
            crawl_run_id=run_id,
            store_id=store_id,
            parser_version=connector.parser_version,
            captured_at=as_of,
        )
        result.captures += 1
        if fetched.is_block_page:
            any_block_page = True
            result.errors += 1
            continue
        if not fetched.ok:
            result.errors += 1
            continue
        result.fetched += 1

        parsed = connector.parse_product(fetched)
        if not parsed.ok:
            result.errors += 1
            continue
        raw_by_id = _raw_by_external_id(fetched.payload)
        for obs in parsed.observations:
            obs = _stamp(obs, as_of)
            result.parsed += 1
            variant = _resolve_variant(
                db, retailer.id, obs, raw_by_id.get(obs.variant_ref), as_of=as_of
            )
            validation = connector.validate_observation(obs)
            pending.append(
                _PendingObservation(
                    obs=obs,
                    variant=variant,
                    raw_capture_id=capture.id,
                    validation_ok=validation.valid,
                )
            )

    # -- anomaly detection (batch + per-variant) --------------------------------------- #
    prior = _build_prior_stats(db, retailer.id, store_id, pending)
    batch = Batch(
        observations=tuple(p.obs for p in pending),
        is_block_page=any_block_page,
        parser_returned_zero=(result.discovered > 0 and not pending and not any_block_page),
        packages={},
        external_products={},
    )
    anomalies = detector.detect(batch, prior)
    batch_quarantine, per_ref_quarantine = _quarantine_targets(anomalies)

    # -- record into append-only history ---------------------------------------------- #
    for item in pending:
        quarantined, anomaly_type = _decide_quarantine(
            item, anomalies, batch_quarantine, per_ref_quarantine
        )
        row = record_observation(
            db,
            item.obs,
            product_variant_id=item.variant.id,
            retailer_id=retailer.id,
            as_of=as_of,
            store_id=store_id,
            crawl_run_id=run_id,
            raw_capture_id=item.raw_capture_id,
            quarantined=quarantined,
            anomaly_type=anomaly_type,
        )
        result.observations.append(row)
        if quarantined:
            result.quarantined += 1
        elif item.validation_ok:
            result.accepted += 1
        else:
            result.rejected += 1

    # -- roll counters into the CrawlRun ----------------------------------------------- #
    if service is not None and crawl_run is not None:
        service.record(
            crawl_run,
            discovered=result.discovered,
            fetched=result.fetched,
            parsed=result.parsed,
            accepted=result.accepted,
            rejected=result.rejected,
            quarantined=result.quarantined,
            errors=result.errors,
        )

    # -- coverage snapshot + engine projection ----------------------------------------- #
    result.coverage = PriceCoverageService().snapshot(
        db, retailer.id, store_id=store_id, as_of=as_of
    )
    result.projected = CurrentPriceService().project_current_prices(db, retailer.id)
    return result


def run_crawl_job(
    db: Session,
    job: CrawlJob,
    *,
    connector: RetailerConnector,
    as_of: datetime | None = None,
    detector: AnomalyDetector | None = None,
) -> JobOutcome:
    """Dispatch one crawl job to the price-sync pipeline, isolating any failure.

    Loads the job's :class:`CrawlRun` (and its retailer/store), marks the run running,
    executes :func:`run_price_sync`, then completes the run. Any error is captured and
    returned as a failed :class:`JobOutcome` so the worker's circuit breaker/backoff apply
    and no other job is affected.
    """
    as_of = as_of or _now()
    run = db.get(CrawlRun, job.crawl_run_id)
    if run is None:
        return JobOutcome(ok=False, error=f"crawl_run {job.crawl_run_id} not found")
    retailer = db.get(Retailer, run.retailer_id)
    if retailer is None:
        return JobOutcome(ok=False, error=f"retailer {run.retailer_id} not found")
    store = db.get(Store, run.store_id) if run.store_id is not None else None

    service = CrawlRunService(db)
    try:
        service.start(run, now=as_of)
        if run.connector_version is None:
            run.connector_version = connector.connector_version
        if run.parser_version is None:
            run.parser_version = connector.parser_version
        db.flush()
        result = run_price_sync(
            db, retailer, store, connector, as_of=as_of, crawl_run=run, detector=detector
        )
        service.complete(run, now=as_of)
    except Exception as exc:
        run.status = RunStatus.FAILED.value
        run.completed_at = as_of
        db.flush()
        return JobOutcome(
            ok=False,
            retailer_code=retailer.slug,
            error=f"{type(exc).__name__}: {exc}",
        )
    return JobOutcome(
        ok=True,
        retailer_code=retailer.slug,
        detail={
            "discovered": result.discovered,
            "accepted": result.accepted,
            "quarantined": result.quarantined,
            "errors": result.errors,
            "coverage_status": result.coverage.status if result.coverage else None,
            "projected": result.projected,
        },
    )


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _stamp(obs: NormalizedObservation, as_of: datetime) -> NormalizedObservation:
    """Ensure the observation's ``observed_at`` matches this run's effective instant."""
    if obs.observed_at == as_of:
        return obs
    from dataclasses import replace

    return replace(obs, observed_at=as_of)


def _as_id_list(payload: object) -> list[str]:
    if isinstance(payload, (list, tuple)):
        return [str(x) for x in payload]
    return []


def _raw_by_external_id(payload: object) -> dict[str, dict[str, object]]:
    """Index a fetch payload's raw record(s) by their ``external_id`` for variant resolution."""
    rows = payload if isinstance(payload, (list, tuple)) else [payload]
    out: dict[str, dict[str, object]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("external_id") is not None:
            out[str(row["external_id"])] = row
    return out


def _http_result_from_fetch(fetched: object, external_id: str) -> HttpFetchResult:
    """Adapt a connector :class:`FetchResult` into an :class:`HttpFetchResult` for capture.

    The :class:`RawCaptureRepository` stores an ``HttpFetchResult`` (redacting headers and
    choosing a retention policy by outcome); the demo has no real HTTP result, so we build a
    faithful synthetic one from the fixture fetch.
    """
    url = getattr(fetched, "url", None) or f"demo://product/{external_id}"
    return HttpFetchResult(
        url=url,
        ok=bool(getattr(fetched, "ok", False)),
        status_code=getattr(fetched, "status_code", None),
        content=getattr(fetched, "content", None),
        content_type=getattr(fetched, "content_type", None),
        body_hash=getattr(fetched, "body_hash", None),
        is_block_page=bool(getattr(fetched, "is_block_page", False)),
        error=getattr(fetched, "error", None),
    )


def _resolve_variant(
    db: Session,
    retailer_id: int,
    obs: NormalizedObservation,
    raw: dict[str, object] | None,
    *,
    as_of: datetime,
) -> ProductVariant:
    """Upsert the ExternalProduct / canonical Product / ProductVariant for an observation.

    Idempotent: an ``(retailer, external_id)`` seen again returns the existing rows (and
    refreshes ``last_seen_at``). A canonical :class:`Product` is created and linked so the
    :class:`CurrentPriceService` projection has a product to write ``ProductPrice`` against.
    """
    external_id = obs.variant_ref
    name, brand, pkg_qty, pkg_unit, pkg_count = _describe(external_id, raw)

    external = db.execute(
        select(ExternalProduct).where(
            ExternalProduct.retailer_id == retailer_id,
            ExternalProduct.external_id == external_id,
        )
    ).scalars().first()
    if external is None:
        external = ExternalProduct(
            retailer_id=retailer_id,
            external_id=external_id,
            external_url=obs.source.source_url if obs.source is not None else None,
            first_seen_at=as_of,
            last_seen_at=as_of,
            active=True,
        )
        db.add(external)
        db.flush()
    else:
        external.last_seen_at = as_of

    if external.canonical_product_id is None:
        product = Product(
            retailer_id=retailer_id,
            external_id=external_id,
            name=name,
            brand=brand,
            package_quantity=pkg_qty,
            package_unit=pkg_unit,
            is_synthetic=True,
        )
        db.add(product)
        db.flush()
        external.canonical_product_id = product.id
        db.flush()
    product_id = external.canonical_product_id

    variant = db.execute(
        select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id,
            ProductVariant.external_product_id == external.id,
        )
    ).scalars().first()
    if variant is None:
        variant = ProductVariant(
            product_id=product_id,
            retailer_id=retailer_id,
            external_product_id=external.id,
            display_name=name,
            package_quantity=pkg_qty,
            package_unit=pkg_unit,
            package_count=pkg_count,
            active=True,
        )
        db.add(variant)
        db.flush()
    return variant


def _describe(
    external_id: str, raw: dict[str, object] | None
) -> tuple[str, str | None, Decimal | None, str | None, int]:
    """Extract ``(name, brand, package_quantity, package_unit, package_count)`` from raw."""
    if not isinstance(raw, dict):
        return external_id, None, None, None, 1
    name = str(raw.get("name") or external_id)
    brand = str(raw["brand"]) if raw.get("brand") else None
    package = raw.get("package") if isinstance(raw.get("package"), dict) else {}
    qty_raw = package.get("quantity") if isinstance(package, dict) else None
    unit = str(package["unit"]) if isinstance(package, dict) and package.get("unit") else None
    count_raw = package.get("count") if isinstance(package, dict) else None
    qty = _to_decimal(qty_raw)
    count = int(count_raw) if isinstance(count_raw, (int, str)) and str(count_raw).isdigit() else 1
    return name, brand, qty, unit, count


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (ValueError, ArithmeticError):
        return None


def _build_prior_stats(
    db: Session,
    retailer_id: int,
    store_id: int | None,
    pending: list[_PendingObservation],
) -> PriorStats:
    """Assemble the last-good snapshot the incoming batch is judged against.

    Prices/units come from each variant's current open (non-disputed) observation, keyed by
    ``variant_ref`` to match the batch; ``catalog_size`` is the count of variants that
    currently carry an open price for this retailer/store.
    """
    prices: dict[str, Decimal] = {}
    units: dict[str, str] = {}
    for item in pending:
        open_row = _current_open(
            db,
            item.variant.id,
            store_id=store_id,
            price_scope=item.obs.price_scope.value,
            price_type=item.obs.price_type.value,
        )
        if open_row is not None:
            prices[item.obs.variant_ref] = open_row.amount
            if open_row.unit_code is not None:
                units[item.obs.variant_ref] = open_row.unit_code

    catalog_size = _open_catalog_size(db, retailer_id, store_id)
    return PriorStats(
        catalog_size=catalog_size,
        prices=prices,
        units=units,
        currency="EUR",
    )


def _current_open(
    db: Session,
    product_variant_id: int,
    *,
    store_id: int | None,
    price_scope: str,
    price_type: str,
) -> PriceObservation | None:
    stmt = (
        select(PriceObservation)
        .where(
            PriceObservation.product_variant_id == product_variant_id,
            PriceObservation.price_scope == price_scope,
            PriceObservation.price_type == price_type,
            PriceObservation.valid_until.is_(None),
            PriceObservation.verification_status != "disputed",
        )
        .order_by(PriceObservation.valid_from.desc(), PriceObservation.id.desc())
        .limit(1)
    )
    if store_id is None:
        stmt = stmt.where(PriceObservation.store_id.is_(None))
    else:
        stmt = stmt.where(PriceObservation.store_id == store_id)
    return db.execute(stmt).scalars().first()


def _open_catalog_size(db: Session, retailer_id: int, store_id: int | None) -> int:
    stmt = (
        select(func.count(func.distinct(PriceObservation.product_variant_id)))
        .where(
            PriceObservation.retailer_id == retailer_id,
            PriceObservation.valid_until.is_(None),
            PriceObservation.verification_status != "disputed",
        )
    )
    if store_id is None:
        stmt = stmt.where(PriceObservation.store_id.is_(None))
    else:
        stmt = stmt.where(PriceObservation.store_id == store_id)
    return int(db.execute(stmt).scalar() or 0)


def _quarantine_targets(anomalies: list[Anomaly]) -> tuple[bool, set[str]]:
    """Split severe anomalies into a batch-wide flag and the set of per-variant refs."""
    batch_quarantine = False
    per_ref: set[str] = set()
    threshold = _SEVERITY_RANK[_QUARANTINE_SEVERITY]
    for anomaly in anomalies:
        if _SEVERITY_RANK[anomaly.severity] < threshold:
            continue
        if anomaly.variant_ref:
            per_ref.add(anomaly.variant_ref)
        else:
            batch_quarantine = True
    return batch_quarantine, per_ref


def _decide_quarantine(
    item: _PendingObservation,
    anomalies: list[Anomaly],
    batch_quarantine: bool,
    per_ref_quarantine: set[str],
) -> tuple[bool, str]:
    """Decide whether an observation is quarantined and label the anomaly reason."""
    ref = item.obs.variant_ref
    if batch_quarantine:
        reason = _batch_reason(anomalies)
        return True, reason
    if ref in per_ref_quarantine:
        return True, _ref_reason(anomalies, ref)
    if not item.validation_ok:
        return True, "validation_failed"
    return False, "quarantined"


def _batch_reason(anomalies: list[Anomaly]) -> str:
    threshold = _SEVERITY_RANK[_QUARANTINE_SEVERITY]
    for anomaly in anomalies:
        if _SEVERITY_RANK[anomaly.severity] >= threshold and not anomaly.variant_ref:
            return _anomaly_label(anomaly)
    return "batch_quarantined"


def _ref_reason(anomalies: list[Anomaly], ref: str) -> str:
    threshold = _SEVERITY_RANK[_QUARANTINE_SEVERITY]
    for anomaly in anomalies:
        if anomaly.variant_ref == ref and _SEVERITY_RANK[anomaly.severity] >= threshold:
            return _anomaly_label(anomaly)
    return "quarantined"


def _anomaly_label(anomaly: Anomaly) -> str:
    # ``anomaly_type`` is an ``AnomalyType`` (StrEnum -> str() is its value) or a free-text kind.
    return str(anomaly.anomaly_type)


__all__ = [
    "PriceSyncResult",
    "run_crawl_job",
    "run_price_sync",
]
