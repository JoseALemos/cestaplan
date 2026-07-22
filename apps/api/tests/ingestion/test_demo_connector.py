"""Contract-level behaviour of the DemoFixtureConnector (pure fixtures, no DB, no network).

Covers capabilities/policy honesty, store resolution, discovery, the fetch -> parse ->
normalize -> validate chain on the synthetic fixtures (units, unit prices, promotions), and
the "problem" fixtures: a block-page response fails validation (routing to quarantine) and a
catalog-drop scenario collapses discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion import (
    AnomalyType,
    ConnectorStatus,
    LegalStatus,
    NormalizedObservation,
    PriceScope,
    PriceType,
    PromotionType,
)
from cestaplan_api.ingestion.connectors.demo import (
    DEMO_PRODUCTS,
    SCENARIO_BASELINE,
    SCENARIO_BLOCK_PAGE,
    SCENARIO_CATALOG_DROP,
    DemoFixtureConnector,
    observations_for,
)
from cestaplan_api.ingestion.validation import ObservationValidator, ValidationContext


def _obs_for(connector: DemoFixtureConnector, external_id: str) -> NormalizedObservation:
    parsed = connector.parse_product(connector.fetch_product(external_id))
    assert parsed.ok
    assert len(parsed.observations) == 1
    return parsed.observations[0]


def test_capabilities_are_honest() -> None:
    caps = DemoFixtureConnector().capabilities()
    assert caps.full_catalog is True
    assert caps.prices is True
    assert caps.promotions is True
    assert caps.exact_store_scope is True
    # Honest about what the fixtures do not model.
    assert caps.loyalty_prices is False
    assert caps.barcodes is False
    assert caps.nutrition is False
    assert caps.incremental_sync is False
    assert caps.national_scope is False


def test_source_policy_is_public_and_polite() -> None:
    policy = DemoFixtureConnector().source_policy()
    assert policy.legal_status is LegalStatus.PUBLIC
    assert policy.respects_robots is True
    assert policy.allowed_domains == ()  # no real domain to talk to


def test_health_check_reports_active() -> None:
    health = DemoFixtureConnector().health_check()
    assert health.ok is True
    assert health.status is ConnectorStatus.ACTIVE
    assert health.supported is True


def test_resolve_store_is_exact_store_scope() -> None:
    resolution = DemoFixtureConnector().resolve_store(postal_code="28001")
    assert resolution.ok is True
    assert resolution.scope is PriceScope.EXACT_STORE
    assert resolution.external_store_id == "DFM-STORE-001"
    assert resolution.confidence == Decimal("1.0")


def test_discover_products_lists_full_catalog() -> None:
    discovery = DemoFixtureConnector().discover_products()
    assert discovery.ok is True
    assert isinstance(discovery.payload, tuple)
    assert len(discovery.payload) == len(DEMO_PRODUCTS) == 26


def test_fetch_parse_normalize_regular_product() -> None:
    connector = DemoFixtureConnector()
    fetched = connector.fetch_product("DFM-0001")
    assert fetched.ok is True
    assert fetched.content_type == "application/json"
    assert fetched.body_hash  # a synthetic capture still carries a content hash

    obs = _obs_for(connector, "DFM-0001")
    assert obs.variant_ref == "DFM-0001"
    assert obs.amount == Decimal("0.89")
    assert obs.currency == "EUR"
    assert obs.price_scope is PriceScope.EXACT_STORE
    assert obs.price_type is PriceType.REGULAR
    assert obs.unit_code == "l"
    assert obs.unit_amount == Decimal("0.8900")  # 0.89 / 1 L
    assert obs.source is not None
    assert obs.source.connector_version == connector.connector_version


def test_multipack_unit_price_uses_total_base_quantity() -> None:
    # Yogur 4 x 125 g = 500 g -> 1.35 / 0.5 kg = 2.70 €/kg.
    obs = _obs_for(DemoFixtureConnector(), "DFM-0003")
    assert obs.unit_code == "kg"
    assert obs.unit_amount == Decimal("2.7000")


def test_nxm_promotion_is_parsed() -> None:
    obs = _obs_for(DemoFixtureConnector(), "DFM-0003")  # "2x1"
    assert obs.price_type is PriceType.PROMOTIONAL
    assert obs.promotion is not None
    assert obs.promotion.promotion_type is PromotionType.NXM
    assert obs.promotion.required_quantity == 2
    assert obs.promotion.charged_quantity == 1


def test_percentage_promotion_is_parsed() -> None:
    obs = _obs_for(DemoFixtureConnector(), "DFM-0006")  # "-30% de descuento"
    assert obs.promotion is not None
    assert obs.promotion.promotion_type is PromotionType.PERCENTAGE
    assert obs.promotion.percentage_discount == Decimal("30")


def test_validate_observation_accepts_clean_fixture() -> None:
    connector = DemoFixtureConnector()
    result = connector.validate_observation(_obs_for(connector, "DFM-0007"))
    assert result.valid is True


def test_deterministic_across_instances() -> None:
    # Same scenario -> byte-identical observations (fix the timestamp, which defaults to now).
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    ids = ["DFM-0005"]
    a = observations_for(DemoFixtureConnector(scenario=SCENARIO_BASELINE), ids, as_of=as_of)
    b = observations_for(DemoFixtureConnector(scenario=SCENARIO_BASELINE), ids, as_of=as_of)
    assert list(a) == list(b)
    assert a[0].amount == Decimal("6.95")


def test_block_page_fixture_fails_validation_and_routes_to_quarantine() -> None:
    connector = DemoFixtureConnector(scenario=SCENARIO_BLOCK_PAGE)
    fetched = connector.fetch_product("DFM-0001")
    assert fetched.ok is False
    assert fetched.is_block_page is True
    assert fetched.status_code == 403

    # A parse of a block-page capture yields no price observations.
    parsed = connector.parse_product(fetched)
    assert parsed.ok is False
    assert parsed.observations == ()

    # An observation whose fetch was a block page fails validation with a BLOCK_PAGE anomaly,
    # which the pipeline routes to quarantine (never treated as a real price).
    clean_obs = _obs_for(DemoFixtureConnector(), "DFM-0001")
    verdict = ObservationValidator().validate(
        clean_obs, ValidationContext(has_store_link=True, is_block_page=True)
    )
    assert verdict.valid is False
    assert verdict.anomaly_type is AnomalyType.BLOCK_PAGE


def test_catalog_drop_scenario_collapses_discovery() -> None:
    discovery = DemoFixtureConnector(scenario=SCENARIO_CATALOG_DROP).discover_products()
    assert isinstance(discovery.payload, tuple)
    assert len(discovery.payload) == 2
