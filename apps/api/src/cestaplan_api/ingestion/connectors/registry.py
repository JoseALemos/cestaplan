"""Connector registry + crawl-worker dispatch hook for the price-ingestion subsystem (FASE B).

The registry maps a ``retailer_code`` to a connector factory. It is the single place the
crawl worker learns which concrete connector serves a retailer, keeping the worker itself
free of any connector knowledge.

The :class:`DemoFixtureConnector` is always registered: the demo is a synthetic, network-free
source and therefore not gated behind operator opt-in the way a real scraping connector is
(``DEMO_ALWAYS_ENABLED``). Real connectors would register here too, gated by a feature flag /
operator configuration.

:func:`build_worker_registry` returns a crawl-worker ``ConnectorRegistry`` whose default
handler resolves the job's retailer, looks up its connector and runs the full orchestration
(:func:`~cestaplan_api.ingestion.orchestration.run_crawl_job`). A retailer with no registered
connector falls through to a safe no-op outcome, and any connector failure is isolated to its
own job by the worker.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OpenPricesAdapter
from cestaplan_api.ingestion import RetailerConnector
from cestaplan_api.ingestion.connectors.demo import DemoFixtureConnector
from cestaplan_api.ingestion.connectors.openprices import OpenPricesConnector
from cestaplan_api.ingestion.crawl_worker import ConnectorRegistry, JobOutcome
from cestaplan_api.ingestion.orchestration import run_crawl_job
from cestaplan_api.models import CrawlJob, CrawlRun, Retailer, Store
from cestaplan_api.services.open_prices_sync import (
    open_prices_enabled,
    parse_osm_from_external_code,
)

#: The demo connector is synthetic and safe, so it is never gated off.
DEMO_ALWAYS_ENABLED = True

#: A connector factory takes optional keyword args (e.g. ``scenario``) and returns a connector.
ConnectorFactory = Callable[..., RetailerConnector]

#: retailer_code -> connector factory. Real connectors register here behind feature flags.
CONNECTOR_FACTORIES: dict[str, ConnectorFactory] = {}


def register_connector(retailer_code: str, factory: ConnectorFactory) -> None:
    """Register a connector factory for a retailer code (idempotent overwrite)."""
    CONNECTOR_FACTORIES[retailer_code] = factory


def get_connector(retailer_code: str, **kwargs: object) -> RetailerConnector | None:
    """Instantiate the connector registered for ``retailer_code``, or ``None`` if unknown."""
    factory = CONNECTOR_FACTORIES.get(retailer_code)
    if factory is None:
        return None
    return factory(**kwargs)


if DEMO_ALWAYS_ENABLED:
    register_connector(DemoFixtureConnector.retailer_code, DemoFixtureConnector)

#: The first real connector: Open Food Facts Open Prices (legal, ODbL open dataset). Registered
#: under its retailer code; runtime use stays gated by the Open Prices DataSource.is_enabled flag
#: (see :func:`build_open_prices_connector`).
register_connector(OpenPricesConnector.retailer_code, OpenPricesConnector)


def build_open_prices_connector(
    db: Session, store: Store, *, adapter: OpenPricesAdapter | None = None
) -> OpenPricesConnector | None:
    """Build an :class:`OpenPricesConnector` for a store, gated by the OP ``DataSource``.

    Resolves the store's OSM location from its ``external_code`` (``osm:{TYPE}/{id}``) and honours
    the existing Open Prices ``DataSource.is_enabled`` flag (a disabled source yields a disabled
    connector). Returns ``None`` when the store has no usable OSM location. Reuses the shared
    :class:`~cestaplan_api.adapters.openprices.OpenPricesAdapter` (injectable for tests).
    """
    osm = parse_osm_from_external_code(store.external_code)
    if osm is None:
        return None
    osm_id, osm_type = osm
    return OpenPricesConnector(
        osm_id=osm_id,
        osm_type=osm_type,
        adapter=adapter,
        enabled=open_prices_enabled(db),
    )


def build_worker_registry(*, as_of: datetime | None = None) -> ConnectorRegistry:
    """Return a crawl-worker :class:`ConnectorRegistry` that dispatches to orchestration.

    The default handler resolves the job's run -> retailer, keys the registry by the
    retailer's ``slug`` (its stable code), instantiates the connector (honouring an optional
    ``scenario`` in the job payload) and runs the full pipeline. Unknown retailers are a safe
    no-op so the worker never crashes on an unregistered connector.
    """

    def handler(db: Session, job: CrawlJob) -> JobOutcome:
        run = db.get(CrawlRun, job.crawl_run_id)
        if run is None:
            return JobOutcome(ok=False, error=f"crawl_run {job.crawl_run_id} not found")
        retailer = db.get(Retailer, run.retailer_id)
        if retailer is None:
            return JobOutcome(ok=False, error=f"retailer {run.retailer_id} not found")

        scenario = _scenario_from_payload(job)
        connector = get_connector(retailer.slug, **scenario)
        if connector is None:
            return JobOutcome(
                ok=True,
                retailer_code=retailer.slug,
                detail={"skipped": f"no connector registered for {retailer.slug!r}"},
            )
        return run_crawl_job(db, job, connector=connector, as_of=as_of)

    return ConnectorRegistry(default=handler)


def _scenario_from_payload(job: CrawlJob) -> dict[str, str]:
    """Extract a connector ``scenario`` kwarg from the job payload, if present."""
    payload = job.payload or {}
    scenario = payload.get("scenario") if isinstance(payload, dict) else None
    return {"scenario": str(scenario)} if scenario else {}


__all__ = [
    "CONNECTOR_FACTORIES",
    "DEMO_ALWAYS_ENABLED",
    "ConnectorFactory",
    "build_open_prices_connector",
    "build_worker_registry",
    "get_connector",
    "register_connector",
]
