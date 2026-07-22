"""Connector-contract behaviour (pure, no DB, no network).

A trivial in-memory connector implements only the required members; every optional method
must return a controlled "not supported" result and never raise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cestaplan_api.ingestion import (
    Capabilities,
    ConnectorStatus,
    FetchResult,
    HealthResult,
    LegalStatus,
    NormalizedObservation,
    ParseResult,
    PriceScope,
    PriceType,
    RetailerConnector,
    SourcePolicy,
    StoreResolutionResult,
    ValidationResult,
    enum_values,
)


class _TrivialConnector(RetailerConnector):
    """Minimal connector: declares identity + capabilities/policy, inherits the rest."""

    retailer_code = "trivial"
    connector_version = "1.2.3"
    parser_version = "4.5.6"

    def capabilities(self) -> Capabilities:
        return Capabilities(prices=True, national_scope=True)

    def source_policy(self) -> SourcePolicy:
        return SourcePolicy(
            allowed_domains=("example.test",),
            request_delay=2.0,
            max_concurrency=1,
            legal_status=LegalStatus.PUBLIC,
            contact="ops@example.test",
        )


def test_required_members() -> None:
    c = _TrivialConnector()
    assert c.retailer_code == "trivial"
    assert c.connector_version == "1.2.3"
    assert c.parser_version == "4.5.6"

    caps = c.capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.prices is True
    assert caps.national_scope is True
    assert caps.promotions is False  # defaults off

    policy = c.source_policy()
    assert isinstance(policy, SourcePolicy)
    assert policy.legal_status is LegalStatus.PUBLIC
    assert policy.allowed_domains == ("example.test",)


def test_default_methods_return_controlled_unsupported_results() -> None:
    """Every optional method returns a controlled result and never raises."""
    c = _TrivialConnector()

    health = c.health_check()
    assert isinstance(health, HealthResult)
    assert health.status is ConnectorStatus.UNSUPPORTED
    assert health.supported is False

    resolve = c.resolve_store(postal_code="28001")
    assert isinstance(resolve, StoreResolutionResult)
    assert resolve.supported is False
    assert resolve.scope is PriceScope.UNKNOWN

    for fetch in (
        c.discover_stores(),
        c.discover_products(),
        c.fetch_product("x"),
        c.fetch_category("y"),
        c.fetch_offers(),
    ):
        assert isinstance(fetch, FetchResult)
        assert fetch.ok is False
        assert fetch.supported is False

    for parse in (c.parse_product(object()), c.normalize_product(object())):
        assert isinstance(parse, ParseResult)
        assert parse.supported is False
        assert parse.observations == ()

    obs = NormalizedObservation(
        variant_ref="EXT-1",
        amount=Decimal("1.50"),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        price_type=PriceType.REGULAR,
        observed_at=datetime.now(UTC),
    )
    validation = c.validate_observation(obs)
    assert isinstance(validation, ValidationResult)
    assert validation.supported is False

    assert c.get_next_cursor(None) is None
    assert c.supports_incremental_sync() is False
    assert c.supports_conditional_requests() is False


def test_enum_values_helper_matches_enum() -> None:
    assert enum_values(PriceScope) == (
        "exact_store",
        "delivery_zone",
        "postal_code",
        "municipality",
        "province",
        "region",
        "national",
        "unknown",
    )


def test_normalized_observation_keeps_decimal_money() -> None:
    obs = NormalizedObservation(
        variant_ref="EXT-1",
        amount=Decimal("2.49"),
        currency="EUR",
        price_scope=PriceScope.EXACT_STORE,
        price_type=PriceType.PROMOTIONAL,
        observed_at=datetime.now(UTC),
    )
    assert isinstance(obs.amount, Decimal)
    assert obs.confidence == Decimal("1.0")
