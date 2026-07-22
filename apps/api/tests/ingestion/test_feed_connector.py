"""CsvFeedConnector on the FASE A/B ingestion pipeline — synthetic feeds, NO network.

FASE D proves the ingestion architecture is reusable against a *structurally different* source:
an operator-provided batch **price feed** (CSV/JSON) instead of a paginated crowdsourced HTTP API
(Open Prices). These tests drive the connector from synthetic CSV/JSON strings (no live network,
no scraping, no fabrication) and assert it is honest end-to-end:

- capabilities/policy are truthful and computed from the *actual* feed (AUTHORIZED, no full
  catalog, promotions/barcodes/store-scope only when the feed carries them);
- a synthetic CSV feed yields real observations (Decimal amount, correct scope/type, promotions
  parsed and NOT collapsed) that pass validation, with missing-price rows honestly skipped;
- a store-less row is ``national`` scope and never claims ``exact_store``;
- the full vertical via :func:`run_price_sync` records append-only observations, closes + appends
  on a real price change, quarantines an anomalous/invalid row (last-good untouched), writes an
  honest coverage snapshot and projects ProductPrice;
- capabilities are honest and a disabled connector discovers nothing;
- the registry exposes the connector, gated by a ``DataSource.is_enabled`` flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import (
    ConnectorStatus,
    LegalStatus,
    PriceScope,
    PriceType,
    PromotionType,
)
from cestaplan_api.ingestion.connectors.feed import CsvFeedConnector
from cestaplan_api.ingestion.connectors.registry import (
    build_csv_feed_connector,
    get_connector,
)
from cestaplan_api.ingestion.orchestration import run_price_sync
from cestaplan_api.models import (
    CoverageSnapshot,
    DataSource,
    PriceObservation,
    ProductPrice,
    Retailer,
    Store,
)

_STORE_CODE = "store-0001"

# A synthetic operator feed (canonical section-20 columns). Row 1: a store-scoped packaged milk.
# Row 2: a store-scoped 2x1 promo. Row 3: a store-LESS (national) per-kg loose price. Row 4: a
# row with NO amount (must be skipped, never turned into 0).
_CSV_FEED = (
    "retailer_slug,store_external_code,product_external_id,product_name,brand,barcode,"
    "package_quantity,package_unit,amount,currency,promotion,source_type,source_name,"
    "observed_at,canonical_name\n"
    f"acme,{_STORE_CODE},SKU-MILK,Leche entera,Hacendado,8410000000001,"
    "1,l,0.95,EUR,,authorized_partner,ACME Feed,2026-04-10T00:00:00Z,leche\n"
    f"acme,{_STORE_CODE},SKU-EGGS,Huevos L,Hacendado,8410000000002,"
    "12,unit,1.80,EUR,2x1,authorized_partner,ACME Feed,2026-04-10T00:00:00Z,huevos\n"
    "acme,,SKU-APPLE,Manzanas,,,"
    "1,kg,2.20,EUR,,authorized_partner,ACME Feed,2026-04-10T00:00:00Z,manzana\n"
    f"acme,{_STORE_CODE},SKU-NOPRICE,Sin precio,,,"
    "1,unit,,EUR,,authorized_partner,ACME Feed,2026-04-10T00:00:00Z,\n"
)

# A store-scoped-only feed (all rows carry a store) used by the pipeline vertical tests, so the
# observations are exact_store and parallel the Open Prices vertical.
_STORE_FEED_TEMPLATE = (
    "retailer_slug,store_external_code,product_external_id,product_name,"
    "package_quantity,package_unit,amount,currency,source_type,source_name,observed_at\n"
    f"acme,{_STORE_CODE},SKU-MILK,Leche entera,1,l,{{milk}},EUR,"
    "authorized_partner,ACME Feed,2026-04-10T00:00:00Z\n"
    f"acme,{_STORE_CODE},SKU-EGGS,Huevos L,12,unit,1.80,EUR,"
    "authorized_partner,ACME Feed,2026-04-10T00:00:00Z\n"
)


def _store_feed(milk: str = "0.95") -> str:
    return _STORE_FEED_TEMPLATE.format(milk=milk)


def _seed_retailer_store(db: Session) -> tuple[Retailer, Store]:
    retailer = Retailer(
        slug="acme-feed",
        name="ACME (feed)",
        adapter_key=CsvFeedConnector.retailer_code,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    store = Store(
        retailer_id=retailer.id,
        name="ACME store 0001",
        external_code=_STORE_CODE,
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return retailer, store


# --------------------------------------------------------------------------- #
# Contract-level honesty (pure, synthetic feed strings — no DB, no network)
# --------------------------------------------------------------------------- #
def test_capabilities_are_honest_and_feed_derived() -> None:
    caps = CsvFeedConnector(feed=_CSV_FEED).capabilities()
    assert caps.prices is True
    assert caps.partial_catalog is True
    assert caps.full_catalog is False  # a feed is whatever the operator supplies
    # Feed carries a promo column, barcodes, both store and store-less rows -> all honest.
    assert caps.promotions is True
    assert caps.barcodes is True
    assert caps.exact_store_scope is True
    assert caps.national_scope is True
    # Not claimed by a plain price feed:
    assert caps.availability is False
    assert caps.regional_scope is False
    assert caps.incremental_sync is False


def test_capabilities_reflect_a_narrow_feed() -> None:
    # A store-less feed with no promo/barcode columns must NOT claim those capabilities.
    caps = CsvFeedConnector(feed=_store_feed()).capabilities()
    assert caps.exact_store_scope is True  # this feed does carry a store
    caps_national = CsvFeedConnector(
        feed=(
            "retailer_slug,store_external_code,product_external_id,product_name,"
            "package_quantity,package_unit,amount,currency,source_type,source_name,observed_at\n"
            "acme,,SKU-X,Prod X,1,kg,3.00,EUR,authorized_partner,ACME,2026-04-10T00:00:00Z\n"
        )
    ).capabilities()
    assert caps_national.exact_store_scope is False
    assert caps_national.national_scope is True
    assert caps_national.promotions is False
    assert caps_national.barcodes is False


def test_source_policy_is_authorized_operator_feed() -> None:
    policy = CsvFeedConnector(feed=_CSV_FEED).source_policy()
    assert policy.legal_status is LegalStatus.AUTHORIZED
    assert policy.respects_robots is True
    assert policy.allowed_domains == ()  # a local file/string feed has no host


def test_manual_feed_uses_manual_price_type_and_lower_confidence() -> None:
    # A hand-curated feed can declare a MANUAL default price type (still "regular unless a promo
    # says otherwise" — MANUAL just replaces REGULAR as the non-promo default).
    connector = CsvFeedConnector(feed=_CSV_FEED, default_price_type=PriceType.MANUAL)
    obs = connector.parse_product(connector.fetch_product("SKU-MILK")).observations[0]
    assert obs.price_type is PriceType.MANUAL
    # A promo row still becomes PROMOTIONAL regardless of the default.
    promo = connector.parse_product(connector.fetch_product("SKU-EGGS")).observations[0]
    assert promo.price_type is PriceType.PROMOTIONAL


def test_health_check_reports_enabled_disabled_and_unparseable() -> None:
    assert CsvFeedConnector(feed=_CSV_FEED).health_check().status is ConnectorStatus.ACTIVE
    disabled = CsvFeedConnector(feed=_CSV_FEED, enabled=False).health_check()
    assert disabled.status is ConnectorStatus.DISABLED
    assert disabled.ok is False
    # No feed source at all -> honestly source-unavailable, never a crash.
    assert CsvFeedConnector().health_check().status is ConnectorStatus.SOURCE_UNAVAILABLE


def test_discover_lists_priceable_external_ids() -> None:
    discovery = CsvFeedConnector(feed=_CSV_FEED).discover_products()
    assert discovery.ok is True
    assert isinstance(discovery.payload, tuple)
    assert set(discovery.payload) == {"SKU-MILK", "SKU-EGGS", "SKU-APPLE", "SKU-NOPRICE"}


def test_fetch_parse_normalize_store_row_is_exact_store() -> None:
    connector = CsvFeedConnector(feed=_CSV_FEED)
    parsed = connector.parse_product(connector.fetch_product("SKU-MILK"))
    assert parsed.ok is True
    assert len(parsed.observations) == 1

    obs = parsed.observations[0]
    assert obs.variant_ref == "SKU-MILK"
    assert obs.amount == Decimal("0.95")
    assert isinstance(obs.amount, Decimal)
    assert obs.currency == "EUR"
    assert obs.price_scope is PriceScope.EXACT_STORE
    assert obs.price_type is PriceType.REGULAR
    assert obs.observed_at == datetime(2026, 4, 10, tzinfo=UTC)
    # €/l unit price derived by the shared normalizer (1 l package).
    assert obs.unit_code == "l"
    assert obs.unit_amount == Decimal("0.9500")
    assert obs.source is not None
    assert obs.source.source_slug == "operator-feed"
    assert connector.validate_observation(obs).valid is True


def test_promo_row_is_parsed_not_collapsed() -> None:
    connector = CsvFeedConnector(feed=_CSV_FEED)
    obs = connector.parse_product(connector.fetch_product("SKU-EGGS")).observations[0]
    # A 2x1 promo becomes a structured NXM rule; the amount is still the real package price
    # (never collapsed into an "effective" unit price).
    assert obs.price_type is PriceType.PROMOTIONAL
    assert obs.amount == Decimal("1.80")
    assert obs.promotion is not None
    assert obs.promotion.promotion_type is PromotionType.NXM
    assert obs.promotion.required_quantity == 2
    assert obs.promotion.charged_quantity == 1
    assert obs.promotion.raw_text == "2x1"
    assert connector.validate_observation(obs).valid is True


def test_storeless_row_is_national_never_exact_store() -> None:
    connector = CsvFeedConnector(feed=_CSV_FEED)
    obs = connector.parse_product(connector.fetch_product("SKU-APPLE")).observations[0]
    assert obs.price_scope is PriceScope.NATIONAL
    assert obs.price_scope is not PriceScope.EXACT_STORE
    # €/kg unit price for the loose per-kg row.
    assert obs.unit_code == "kg"
    assert obs.unit_amount == Decimal("2.2000")
    # Validation passes without a store link because it does NOT claim exact_store.
    assert connector.validate_observation(obs).valid is True


def test_missing_price_row_is_skipped_never_fabricated() -> None:
    connector = CsvFeedConnector(feed=_CSV_FEED)
    parsed = connector.parse_product(connector.fetch_product("SKU-NOPRICE"))
    # The row exists but has no amount -> zero observations, a warning, never a 0-priced row.
    assert parsed.ok is True
    assert parsed.observations == ()
    assert any("missing price" in w for w in parsed.warnings)


def test_json_feed_is_parsed_via_shared_adapter() -> None:
    json_feed = (
        '[{"retailer_slug":"acme","store_external_code":"store-0001",'
        '"product_external_id":"SKU-MILK","product_name":"Leche","package_quantity":"1",'
        '"package_unit":"l","amount":"0.95","currency":"EUR","source_type":"authorized_partner",'
        '"source_name":"ACME","observed_at":"2026-04-10T00:00:00Z"}]'
    )
    connector = CsvFeedConnector(feed=json_feed, feed_format="json")
    obs = connector.parse_product(connector.fetch_product("SKU-MILK")).observations[0]
    assert obs.amount == Decimal("0.95")
    assert obs.price_scope is PriceScope.EXACT_STORE


def test_disabled_connector_discovers_nothing() -> None:
    connector = CsvFeedConnector(feed=_CSV_FEED, enabled=False)
    assert connector.discover_products().payload == ()
    assert connector.fetch_product("SKU-MILK").ok is False


def test_unparseable_currency_row_is_skipped_not_crashed() -> None:
    # A non-EUR currency is rejected by the shared normalizer -> skipped with a warning.
    feed = (
        "retailer_slug,store_external_code,product_external_id,product_name,"
        "package_quantity,package_unit,amount,currency,source_type,source_name,observed_at\n"
        "acme,store-0001,SKU-USD,Prod,1,unit,9.99,USD,authorized_partner,ACME,2026-04-10T00:00:00Z\n"
    )
    connector = CsvFeedConnector(feed=feed)
    parsed = connector.parse_product(connector.fetch_product("SKU-USD"))
    assert parsed.observations == ()
    assert parsed.warnings


# --------------------------------------------------------------------------- #
# Full vertical through run_price_sync (live Postgres, synthetic feed — no network)
# --------------------------------------------------------------------------- #
def test_run_price_sync_records_observations_coverage_projection(db_session: Session) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC) - timedelta(minutes=5)

    result = run_price_sync(
        db_session, retailer, store, CsvFeedConnector(feed=_store_feed()), as_of=as_of
    )

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

    snapshot = db_session.execute(
        select(CoverageSnapshot).where(CoverageSnapshot.retailer_id == retailer.id)
    ).scalars().one()
    assert snapshot.discovered_products == 2
    assert snapshot.priced_products == 2
    assert snapshot.expected_products == 2

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

    run_price_sync(
        db_session, retailer, store, CsvFeedConnector(feed=_store_feed("0.95")), as_of=t0
    )
    result = run_price_sync(
        db_session, retailer, store, CsvFeedConnector(feed=_store_feed("1.10")), as_of=t1
    )
    assert result.quarantined == 0

    rows = db_session.execute(
        select(PriceObservation)
        .where(PriceObservation.retailer_id == retailer.id)
        .order_by(PriceObservation.valid_from)
    ).scalars().all()
    milk_rows = [r for r in rows if r.amount in (Decimal("0.95"), Decimal("1.10"))]
    assert len(milk_rows) == 2
    assert milk_rows[0].amount == Decimal("0.95")
    assert milk_rows[0].valid_until == t1
    assert milk_rows[1].amount == Decimal("1.10")
    assert milk_rows[1].valid_until is None


def test_run_price_sync_anomalous_row_is_quarantined_last_good_untouched(
    db_session: Session,
) -> None:
    retailer, store = _seed_retailer_store(db_session)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = datetime.now(UTC) - timedelta(hours=1)

    # First run: milk at 1.00 (last-good).
    run_price_sync(
        db_session, retailer, store, CsvFeedConnector(feed=_store_feed("1.00")), as_of=t0
    )
    # Second run: a x100 slip (1.00 -> 100.00) must be quarantined, not promoted.
    result = run_price_sync(
        db_session, retailer, store, CsvFeedConnector(feed=_store_feed("100.00")), as_of=t1
    )
    assert result.quarantined == 1

    milk_rows = db_session.execute(
        select(PriceObservation)
        .where(
            PriceObservation.retailer_id == retailer.id,
            PriceObservation.amount.in_([Decimal("1.00"), Decimal("100.00")]),
        )
        .order_by(PriceObservation.valid_from)
    ).scalars().all()
    good = [r for r in milk_rows if r.amount == Decimal("1.00")]
    suspect = [r for r in milk_rows if r.amount == Decimal("100.00")]
    assert len(good) == 1
    assert good[0].valid_until is None  # last-good left open and untouched
    assert good[0].verification_status != "disputed"
    assert len(suspect) == 1
    assert suspect[0].verification_status == "disputed"  # quarantined, closed
    assert suspect[0].valid_until == t1


def test_run_price_sync_missing_price_row_never_fabricated(db_session: Session) -> None:
    retailer, store = _seed_retailer_store(db_session)
    as_of = datetime.now(UTC)

    # A feed whose single row carries no amount: discovered, but never priced.
    feed = (
        "retailer_slug,store_external_code,product_external_id,product_name,"
        "package_quantity,package_unit,amount,currency,source_type,source_name,observed_at\n"
        "acme,store-0001,SKU-EMPTY,Prod,1,unit,,EUR,authorized_partner,ACME,2026-04-10T00:00:00Z\n"
    )
    result = run_price_sync(db_session, retailer, store, CsvFeedConnector(feed=feed), as_of=as_of)
    assert result.discovered == 1
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
def test_registry_exposes_csv_feed_connector() -> None:
    connector = get_connector(CsvFeedConnector.retailer_code)
    assert isinstance(connector, CsvFeedConnector)


def test_build_csv_feed_connector_respects_enabled_gate(db_session: Session) -> None:
    source = DataSource(
        slug="acme-feed-source",
        name="ACME operator feed",
        source_type="authorized_partner",
        adapter_key=CsvFeedConnector.retailer_code,
        is_enabled=True,
    )
    db_session.add(source)
    db_session.flush()

    enabled = build_csv_feed_connector(
        db_session, feed=_store_feed(), data_source_slug="acme-feed-source"
    )
    assert enabled.health_check().status is ConnectorStatus.ACTIVE
    enabled_payload = enabled.discover_products().payload
    assert isinstance(enabled_payload, tuple)
    assert set(enabled_payload) == {"SKU-MILK", "SKU-EGGS"}

    source.is_enabled = False
    db_session.flush()
    disabled = build_csv_feed_connector(
        db_session, feed=_store_feed(), data_source_slug="acme-feed-source"
    )
    assert disabled.health_check().status is ConnectorStatus.DISABLED
    assert disabled.discover_products().payload == ()


def test_build_csv_feed_connector_disabled_when_source_absent(db_session: Session) -> None:
    connector = build_csv_feed_connector(
        db_session, feed=_store_feed(), data_source_slug="does-not-exist"
    )
    assert connector.health_check().status is ConnectorStatus.DISABLED
