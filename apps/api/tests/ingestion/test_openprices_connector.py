"""OpenPricesConnector on the FASE A/B ingestion pipeline — HTTPX fully mocked, NO network.

Open Prices is a legal, ODbL open dataset. These tests drive the connector's underlying
:class:`OpenPricesAdapter` through an ``httpx.MockTransport`` (no live Open Prices calls) and
assert the connector is honest end-to-end:

- capabilities/policy are truthful (``full_catalog=False``, ODbL/public, robots-respecting);
- a mocked store payload yields real observations (Decimal amount, barcode, price date,
  ``exact_store`` scope, ODbL price-page source) that pass validation, with NO fabricated
  promotions and barcode-less rows honestly skipped;
- the full vertical via :func:`run_price_sync` records append-only observations, closes +
  appends on a real price change, writes an honest coverage snapshot and projects ProductPrice;
- 404/network failures degrade to an empty result (never a crash, never a fabricated price);
- the registry exposes the connector, gated by the Open Prices ``DataSource.is_enabled`` flag.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.adapters.openprices import OpenPricesAdapter
from cestaplan_api.ingestion import ConnectorStatus, LegalStatus, PriceScope, PriceType
from cestaplan_api.ingestion.connectors.openprices import OpenPricesConnector
from cestaplan_api.ingestion.connectors.registry import (
    build_open_prices_connector,
    get_connector,
)
from cestaplan_api.ingestion.orchestration import run_price_sync
from cestaplan_api.models import (
    CoverageSnapshot,
    PriceObservation,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services.open_prices_sync import ensure_open_prices_data_source

_OSM_ID = 677280352
_OSM_TYPE = "WAY"
_BARCODE_MILK = "8410000000001"
_BARCODE_APPLE = "8410000000002"

# A realistic Open Prices ``/prices`` page for one OSM store. Item 2 has no ``product_code``
# (a loose/category price that must be honestly skipped, never given a fabricated barcode);
# item 3 is a discounted per-kilogram price (the connector still records it as a REGULAR price
# with NO fabricated promotion).
_PAGE: dict[str, object] = {
    "items": [
        {
            "id": 101,
            "product_code": _BARCODE_MILK,
            "product_name": "Leche entera",
            "price": 0.95,
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": None,
            "location_osm_id": _OSM_ID,
            "location_osm_type": _OSM_TYPE,
            "location": {"osm_id": _OSM_ID, "osm_type": _OSM_TYPE, "osm_name": "Lidl"},
        },
        {
            "id": 102,
            "product_code": None,  # loose/category -> no barcode -> skipped, never fabricated
            "product_name": "ESPARRAGO VERDE",
            "price": "3.89",
            "currency": "EUR",
            "date": "2026-04-10",
            "price_per": "UNIT",
        },
        {
            "id": 103,
            "product_code": _BARCODE_APPLE,
            "product_name": "Manzanas",
            "price": 1.80,
            "currency": "EUR",
            "date": "2026-04-11",
            "price_per": "KILOGRAM",
            "price_is_discounted": True,
            "price_without_discount": 2.20,
            "location_osm_id": _OSM_ID,
            "location_osm_type": _OSM_TYPE,
        },
    ],
    "page": 1,
    "pages": 1,
    "size": 100,
    "total": 3,
}

Handler = Callable[[httpx.Request], httpx.Response]


def _ok_handler(request: httpx.Request) -> httpx.Response:
    assert "prices.openfoodfacts.org" in str(request.url)
    return httpx.Response(200, json=_PAGE)


def _connector_with(handler: Handler, *, enabled: bool = True) -> OpenPricesConnector:
    adapter = OpenPricesAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    return OpenPricesConnector(
        osm_id=_OSM_ID, osm_type=_OSM_TYPE, adapter=adapter, enabled=enabled
    )


def _seed_retailer_store(db: Session) -> tuple[Retailer, Store]:
    retailer = Retailer(
        slug="lidl-open-prices",
        name="Lidl (Open Prices)",
        adapter_key=OpenPricesConnector.retailer_code,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(
        retailer_id=retailer.id,
        name="Lidl WAY/677280352",
        external_code=f"osm:{_OSM_TYPE}/{_OSM_ID}",
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return retailer, store


# --------------------------------------------------------------------------- #
# Contract-level honesty (pure, mocked httpx)
# --------------------------------------------------------------------------- #
def test_capabilities_are_honest() -> None:
    caps = OpenPricesConnector().capabilities()
    assert caps.prices is True
    assert caps.barcodes is True
    assert caps.exact_store_scope is True
    assert caps.partial_catalog is True
    # Crowdsourced/sparse: NOT a full catalog, and no promotions/loyalty/national scope.
    assert caps.full_catalog is False
    assert caps.promotions is False
    assert caps.loyalty_prices is False
    assert caps.national_scope is False
    assert caps.regional_scope is False


def test_source_policy_is_public_odbl_and_polite() -> None:
    policy = OpenPricesConnector().source_policy()
    assert policy.legal_status is LegalStatus.PUBLIC
    assert policy.respects_robots is True
    assert policy.allowed_domains == ("prices.openfoodfacts.org",)
    assert policy.contact  # identifiable User-Agent/contact
    assert OpenPricesConnector.license_code == "ODbL"


def test_health_check_reports_enabled_and_disabled() -> None:
    assert OpenPricesConnector(enabled=True).health_check().status is ConnectorStatus.ACTIVE
    disabled = OpenPricesConnector(enabled=False).health_check()
    assert disabled.status is ConnectorStatus.DISABLED
    assert disabled.ok is False


def test_resolve_store_maps_osm_location_to_exact_store() -> None:
    resolution = OpenPricesConnector(osm_id=_OSM_ID, osm_type=_OSM_TYPE).resolve_store()
    assert resolution.ok is True
    assert resolution.scope is PriceScope.EXACT_STORE
    assert resolution.resolved_store_ref == f"osm:{_OSM_TYPE}/{_OSM_ID}"
    assert resolution.confidence == Decimal("1.0")


def test_discover_lists_only_barcoded_prices() -> None:
    connector = _connector_with(_ok_handler)
    discovery = connector.discover_products()
    assert discovery.ok is True
    # Only the two barcoded rows are discovered; the loose (no-barcode) row is skipped.
    assert isinstance(discovery.payload, tuple)
    assert set(discovery.payload) == {_BARCODE_MILK, _BARCODE_APPLE}


def test_fetch_parse_normalize_real_price_with_odbl_source() -> None:
    connector = _connector_with(_ok_handler)
    connector.discover_products()
    parsed = connector.parse_product(connector.fetch_product(_BARCODE_MILK))
    assert parsed.ok is True
    assert len(parsed.observations) == 1

    obs = parsed.observations[0]
    assert obs.variant_ref == _BARCODE_MILK
    assert obs.amount == Decimal("0.95")
    assert isinstance(obs.amount, Decimal)
    assert obs.currency == "EUR"
    assert obs.price_scope is PriceScope.EXACT_STORE
    assert obs.price_type is PriceType.REGULAR
    # observed_at is the REAL price date (Open Prices), midnight UTC.
    assert obs.observed_at == datetime(2026, 4, 10, tzinfo=UTC)
    # ODbL provenance: the source points at the Open Prices price page.
    assert obs.source is not None
    assert obs.source.source_slug == "open-prices"
    assert obs.source.source_url == "https://prices.openfoodfacts.org/prices/101"
    # Validation passes for a real, store-linked price.
    assert connector.validate_observation(obs).valid is True


def test_kilogram_basis_derives_unit_price() -> None:
    connector = _connector_with(_ok_handler)
    connector.discover_products()
    obs = connector.parse_product(connector.fetch_product(_BARCODE_APPLE)).observations[0]
    # price_per=KILOGRAM -> amount is €/kg; the shared normalizer derives a coherent unit price.
    assert obs.unit_code == "kg"
    assert obs.unit_amount == Decimal("1.8000")


def test_discounted_row_never_fabricates_a_promotion() -> None:
    connector = _connector_with(_ok_handler)
    connector.discover_products()
    obs = connector.parse_product(connector.fetch_product(_BARCODE_APPLE)).observations[0]
    # Open Prices flags this row discounted, but the connector records only the REAL price:
    # a REGULAR price with NO fabricated promotion (promotions=False capability).
    assert obs.price_type is PriceType.REGULAR
    assert obs.promotion is None
    assert obs.requires_loyalty is False


def test_graceful_on_404_returns_empty_no_crash() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    connector = _connector_with(handler)
    assert connector.discover_products().payload == ()
    assert connector.fetch_product(_BARCODE_MILK).ok is False


def test_graceful_on_network_error_returns_empty_no_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    connector = _connector_with(handler)
    assert connector.discover_products().payload == ()


def test_disabled_connector_discovers_nothing() -> None:
    connector = _connector_with(_ok_handler, enabled=False)
    assert connector.discover_products().payload == ()
    assert connector.fetch_product(_BARCODE_MILK).ok is False


# --------------------------------------------------------------------------- #
# Full vertical through run_price_sync (live Postgres, mocked httpx)
# --------------------------------------------------------------------------- #
def test_run_price_sync_records_observations_coverage_projection(
    db_session: Session,
) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC) - timedelta(minutes=5)

    result = run_price_sync(
        db_session, retailer, store, _connector_with(_ok_handler), as_of=as_of
    )

    # Two barcoded prices discovered and accepted; the loose row was never a price.
    assert result.discovered == 2
    assert result.accepted == 2
    assert result.quarantined == 0

    observations = db_session.execute(
        select(PriceObservation).where(PriceObservation.retailer_id == retailer.id)
    ).scalars().all()
    assert len(observations) == 2
    assert all(o.valid_until is None for o in observations)  # append-only, all open
    assert all(o.price_scope == PriceScope.EXACT_STORE.value for o in observations)
    assert all(o.store_id == store.id for o in observations)
    assert {o.amount for o in observations} == {Decimal("0.95"), Decimal("1.80")}
    # ODbL price-page provenance is retained on the observation.
    assert all(
        (o.source_url or "").startswith("https://prices.openfoodfacts.org/prices/")
        for o in observations
    )

    # Coverage snapshot is honest: it reports exactly what was discovered/priced (no invented
    # "expected" catalogue), and grades within that known set.
    snapshot = db_session.execute(
        select(CoverageSnapshot).where(CoverageSnapshot.retailer_id == retailer.id)
    ).scalars().one()
    assert snapshot.discovered_products == 2
    assert snapshot.priced_products == 2
    assert snapshot.expected_products == 2

    # ProductPrice projection populated so the meal engine can read current prices.
    projected = db_session.execute(
        select(func.count())
        .select_from(ProductPrice)
        .where(ProductPrice.retailer_id == retailer.id)
    ).scalar_one()
    assert projected == 2
    assert result.projected == 2
    prices = db_session.execute(
        select(ProductPrice).where(ProductPrice.retailer_id == retailer.id)
    ).scalars().all()
    assert all(p.is_synthetic is False for p in prices)


def test_run_price_sync_real_price_change_closes_and_appends(db_session: Session) -> None:
    retailer, store = _seed_retailer_store(db_session)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = datetime.now(UTC) - timedelta(hours=1)

    # First run: milk at 0.95.
    run_price_sync(db_session, retailer, store, _connector_with(_ok_handler), as_of=t0)

    # Second run: the same barcode's price legitimately moved to 1.10.
    changed_page = {**_PAGE, "items": [{**_PAGE["items"][0], "price": 1.10}]}  # type: ignore[index]

    def changed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=changed_page)

    result = run_price_sync(
        db_session, retailer, store, _connector_with(changed_handler), as_of=t1
    )
    assert result.quarantined == 0

    rows = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.retailer_id == retailer.id)
        .order_by(PriceObservation.valid_from)
    ).scalars().all()
    milk_rows = [r for r in rows if r.amount in (Decimal("0.95"), Decimal("1.10"))]
    assert len(milk_rows) == 2
    # The prior interval is closed at t1; a fresh open row carries the new price.
    assert milk_rows[0].amount == Decimal("0.95")
    assert milk_rows[0].valid_until == t1
    assert milk_rows[1].amount == Decimal("1.10")
    assert milk_rows[1].valid_until is None


def test_run_price_sync_empty_source_is_honest_not_fabricated(db_session: Session) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    result = run_price_sync(db_session, retailer, store, _connector_with(handler), as_of=as_of)
    # No prices fabricated when the source has nothing / errors.
    assert result.discovered == 0
    assert result.accepted == 0
    obs_count = db_session.execute(
        select(func.count())
        .select_from(PriceObservation)
        .where(PriceObservation.retailer_id == retailer.id)
    ).scalar_one()
    assert obs_count == 0


# --------------------------------------------------------------------------- #
# Registry registration + DataSource.is_enabled gate
# --------------------------------------------------------------------------- #
def test_registry_exposes_open_prices_connector() -> None:
    connector = get_connector(OpenPricesConnector.retailer_code)
    assert isinstance(connector, OpenPricesConnector)


def test_build_open_prices_connector_respects_enabled_gate(db_session: Session) -> None:
    _retailer, store = _seed_retailer_store(db_session)

    ds = ensure_open_prices_data_source(db_session)
    ds.is_enabled = True
    db_session.flush()
    enabled = build_open_prices_connector(db_session, store)
    assert enabled is not None
    assert enabled.health_check().status is ConnectorStatus.ACTIVE

    ds.is_enabled = False
    db_session.flush()
    disabled = build_open_prices_connector(db_session, store)
    assert disabled is not None
    assert disabled.health_check().status is ConnectorStatus.DISABLED
    assert disabled.discover_products().payload == ()


def test_build_open_prices_connector_none_without_osm_location(db_session: Session) -> None:
    retailer, _store = _seed_retailer_store(db_session)
    bare_store = Store(retailer_id=retailer.id, name="no external code", external_code=None)
    db_session.add(bare_store)
    db_session.flush()
    assert build_open_prices_connector(db_session, bare_store) is None
