"""OFFER connectors (Lidl, Aldi, Deza) on the ingestion pipeline — synthetic, NO network.

FASE E adds three **PARTIAL / OFFER** connectors. These tests prove they are honest and legal:

- capabilities are OFFERS-only (``full_catalog=False``, ``promotions=True``, ``prices=False``);
- a synthetic weekly-leaflet offer fixture yields a promotional ``NormalizedObservation`` whose
  ``PromotionInfo`` carries the promotion's ``valid_from``/``valid_until`` (never collapsed);
- Lidl/Aldi report ``permission_required`` (source policy) and Deza reports an ``unsupported``
  connector status; every live path (health / fetch) returns that controlled result WITHOUT a
  network call;
- the connectors are disabled by default (registry flags off) and then discover nothing;
- the full vertical via :func:`run_price_sync` over an authorized offers fixture records
  promotional observations with validity, an honest (never-complete) coverage snapshot, and
  never claims a full catalogue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import (
    ConnectorStatus,
    LegalStatus,
    PriceScope,
    PriceType,
    PromotionType,
)
from cestaplan_api.ingestion.connectors.offers import (
    AldiOffersConnector,
    DezaOffersConnector,
    LidlOffersConnector,
    _OffersConnector,
)
from cestaplan_api.ingestion.connectors.registry import (
    CONNECTOR_FACTORIES,
    build_aldi_offers_connector,
    build_deza_connector,
    build_lidl_offers_connector,
    get_connector,
)
from cestaplan_api.ingestion.orchestration import run_price_sync
from cestaplan_api.models import CoverageSnapshot, PriceObservation, Retailer

# A synthetic weekly-leaflet offers fixture (Python dicts — NO real HTML/PDF). Row 1: a -20%
# promo. Row 2: a 2x1. Row 3: a loyalty-card price. Row 4: a row with NO promo price (skipped).
_OFFERS: list[dict[str, object]] = [
    {
        "external_id": "OFF-MILK",
        "name": "Leche entera brik 1 L",
        "brand": "Milbona",
        "package": {"quantity": "1", "unit": "l", "count": 1},
        "promo_price": {"amount": "0.79", "currency": "EUR"},
        "regular_price": {"amount": "0.99", "currency": "EUR"},
        "promotion": "-20%",
        "valid_from": "2026-07-20",
        "valid_until": "2026-07-26",
        "loyalty": False,
        "source_url": "leaflet://week-30/milk",
    },
    {
        "external_id": "OFF-EGGS",
        "name": "Huevos L docena",
        "package": {"quantity": "12", "unit": "unit", "count": 1},
        "promo_price": {"amount": "1.80", "currency": "EUR"},
        "promotion": "2x1",
        "valid_from": "2026-07-20",
        "valid_until": "2026-07-26",
        "loyalty": False,
    },
    {
        "external_id": "OFF-COFFEE",
        "name": "Cafe molido 250 g",
        "package": {"quantity": "250", "unit": "g", "count": 1},
        "promo_price": {"amount": "2.50", "currency": "EUR"},
        "promotion": "-15% con tarjeta",
        "valid_from": "2026-07-20",
        "valid_until": "2026-07-26",
        "loyalty": True,
    },
    {
        "external_id": "OFF-NOPRICE",
        "name": "Sin precio de oferta",
        "package": {"quantity": "1", "unit": "unit", "count": 1},
        "promo_price": {"amount": None, "currency": "EUR"},
        "promotion": "-10%",
        "valid_from": "2026-07-20",
        "valid_until": "2026-07-26",
        "loyalty": False,
    },
]

_ALL_CLASSES = (LidlOffersConnector, AldiOffersConnector, DezaOffersConnector)


# --------------------------------------------------------------------------- #
# Honesty — capabilities & policy (pure, synthetic fixture — no DB, no network)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connector_cls", _ALL_CLASSES)
def test_capabilities_are_offers_only_never_full_catalogue(
    connector_cls: type[_OffersConnector],
) -> None:
    caps = connector_cls(offers=_OFFERS, enabled=True).capabilities()
    # An offers leaflet is NOT the whole supermarket.
    assert caps.full_catalog is False
    assert caps.partial_catalog is True
    # Offers carry promo prices, not a full regular-price catalogue.
    assert caps.prices is False
    assert caps.promotions is True
    assert caps.loyalty_prices is True
    # Honest negatives for a leaflet source.
    assert caps.exact_store_scope is False
    assert caps.availability is False
    assert caps.incremental_sync is False


def test_lidl_and_aldi_are_permission_required() -> None:
    for cls in (LidlOffersConnector, AldiOffersConnector):
        policy = cls(enabled=True).source_policy()
        assert policy.legal_status is LegalStatus.PERMISSION_REQUIRED
        assert policy.respects_robots is True
        assert policy.allowed_domains == ()


def test_deza_scraping_is_unsupported_real_path_is_import() -> None:
    connector = DezaOffersConnector(enabled=True)
    # No authorized public source; its live path is UNSUPPORTED (real path is an admin import).
    assert connector.health_check().status is ConnectorStatus.UNSUPPORTED
    assert connector.health_check().supported is False
    live = connector.fetch_offers()
    assert live.supported is False  # unsupported, never a live request
    # Its legal footing still blocks enabling (no authorized public source).
    assert connector.source_policy().legal_status is LegalStatus.PERMISSION_REQUIRED


# --------------------------------------------------------------------------- #
# Live guard — health / fetch return a controlled result, never a network call
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("connector_cls", (LidlOffersConnector, AldiOffersConnector))
def test_live_paths_return_permission_required_without_network(
    connector_cls: type[_OffersConnector],
) -> None:
    connector = connector_cls(enabled=True)  # enabled, but NO authorized fixture -> live path
    health = connector.health_check()
    assert health.status is ConnectorStatus.PERMISSION_REQUIRED
    assert health.ok is False
    # A live fetch is a controlled, not-permitted result — supported-but-refused, no HTTP.
    fetched = connector.fetch_product("anything")
    assert fetched.ok is False
    assert fetched.supported is True
    assert "permission required" in (fetched.error or "").lower()
    offers = connector.fetch_offers()
    assert offers.ok is False and offers.supported is True


@pytest.mark.parametrize("connector_cls", _ALL_CLASSES)
def test_disabled_by_default_discovers_nothing(
    connector_cls: type[_OffersConnector],
) -> None:
    connector = connector_cls(enabled=False)
    assert connector.health_check().status is ConnectorStatus.DISABLED
    assert connector.discover_products().payload == ()


@pytest.mark.parametrize("connector_cls", _ALL_CLASSES)
def test_enabled_without_fixture_still_discovers_nothing(
    connector_cls: type[_OffersConnector],
) -> None:
    # Even enabled, the live path is not permitted — no fixture means nothing to discover.
    assert connector_cls(enabled=True).discover_products().payload == ()


# --------------------------------------------------------------------------- #
# Parse / normalize — promotional observations with validity, never collapsed
# --------------------------------------------------------------------------- #
def test_offer_becomes_promotional_observation_with_validity() -> None:
    connector = LidlOffersConnector(offers=_OFFERS, enabled=True)
    obs = connector.parse_product(connector.fetch_product("OFF-MILK")).observations[0]
    assert obs.variant_ref == "OFF-MILK"
    assert obs.amount == Decimal("0.79")  # the real leaflet promo price, not collapsed
    assert isinstance(obs.amount, Decimal)
    assert obs.price_type is PriceType.PROMOTIONAL
    assert obs.price_scope is PriceScope.NATIONAL
    assert obs.price_scope is not PriceScope.EXACT_STORE
    assert obs.promotion is not None
    assert obs.promotion.promotion_type is PromotionType.PERCENTAGE
    # Validity dates are carried on the structured promotion — not folded into a unit price.
    assert obs.promotion.valid_from == datetime(2026, 7, 20, tzinfo=UTC)
    assert obs.promotion.valid_until == datetime(2026, 7, 26, tzinfo=UTC)
    assert connector.validate_observation(obs).valid is True


def test_2x1_offer_is_modelled_not_collapsed() -> None:
    connector = AldiOffersConnector(offers=_OFFERS, enabled=True)
    obs = connector.parse_product(connector.fetch_product("OFF-EGGS")).observations[0]
    assert obs.amount == Decimal("1.80")  # real package price stays, promo is a rule
    assert obs.promotion is not None
    assert obs.promotion.promotion_type is PromotionType.NXM
    assert obs.promotion.required_quantity == 2
    assert obs.promotion.charged_quantity == 1
    assert obs.promotion.valid_until == datetime(2026, 7, 26, tzinfo=UTC)


def test_loyalty_offer_is_loyalty_type_and_keeps_validity() -> None:
    connector = LidlOffersConnector(offers=_OFFERS, enabled=True)
    obs = connector.parse_product(connector.fetch_product("OFF-COFFEE")).observations[0]
    assert obs.price_type is PriceType.LOYALTY
    assert obs.requires_loyalty is True
    assert obs.promotion is not None
    assert obs.promotion.loyalty_required is True
    assert obs.promotion.valid_from == datetime(2026, 7, 20, tzinfo=UTC)
    assert obs.promotion.valid_until == datetime(2026, 7, 26, tzinfo=UTC)


def test_missing_promo_price_is_skipped_never_zero() -> None:
    connector = LidlOffersConnector(offers=_OFFERS, enabled=True)
    parsed = connector.parse_product(connector.fetch_product("OFF-NOPRICE"))
    assert parsed.observations == ()  # a missing price is never turned into 0
    assert parsed.warnings  # honestly warned


# --------------------------------------------------------------------------- #
# Registry — registered, gated by feature flags (disabled by default)
# --------------------------------------------------------------------------- #
def test_registry_registers_all_three_offer_connectors() -> None:
    assert "lidl_offers" in CONNECTOR_FACTORIES
    assert "aldi_offers" in CONNECTOR_FACTORIES
    assert "deza" in CONNECTOR_FACTORIES


def test_registry_connectors_are_disabled_by_default() -> None:
    # Flags default OFF, so a worker-built connector discovers nothing (never scrapes).
    for code in ("lidl_offers", "aldi_offers", "deza"):
        connector = get_connector(code)
        assert connector is not None
        assert connector.discover_products().payload == ()
        assert connector.health_check().status is ConnectorStatus.DISABLED


def test_registry_builders_accept_authorized_offers_fixture() -> None:
    builders = (
        build_lidl_offers_connector,
        build_aldi_offers_connector,
        build_deza_connector,
    )
    for builder in builders:
        connector = builder(offers=_OFFERS, enabled=True)
        payload = connector.discover_products().payload
        assert isinstance(payload, tuple)
        assert set(payload) == {"OFF-MILK", "OFF-EGGS", "OFF-COFFEE", "OFF-NOPRICE"}


# --------------------------------------------------------------------------- #
# Vertical — run_price_sync over an authorized offers fixture (operator path)
# --------------------------------------------------------------------------- #
def _seed_retailer(db: Session, slug: str) -> Retailer:
    retailer = Retailer(
        slug=slug,
        name=f"{slug} (offers)",
        adapter_key=slug,
        is_synthetic=False,
    )
    db.add(retailer)
    db.flush()
    return retailer


def test_run_price_sync_records_promotional_prices_with_validity(
    db_session: Session,
) -> None:
    retailer = _seed_retailer(db_session, "lidl-offers-test")
    connector = LidlOffersConnector(offers=_OFFERS, enabled=True)
    as_of = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)

    result = run_price_sync(db_session, retailer, None, connector, as_of=as_of)

    # 3 priceable offers recorded (the no-price row is honestly skipped), none full-catalogue.
    assert result.discovered == 4
    assert result.accepted == 3
    assert connector.capabilities().full_catalog is False

    rows = (
        db_session.execute(
            select(PriceObservation).where(
                PriceObservation.retailer_id == retailer.id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    for row in rows:
        assert row.price_type in (PriceType.PROMOTIONAL.value, PriceType.LOYALTY.value)
        assert row.price_scope == PriceScope.NATIONAL.value  # never exact_store
        assert row.store_id is None
        # The promotion's validity window is persisted (not collapsed into a bare price).
        assert row.promotion_valid_from == datetime(2026, 7, 20, tzinfo=UTC)
        assert row.promotion_valid_until == datetime(2026, 7, 26, tzinfo=UTC)


def test_run_price_sync_is_offers_only_never_full_catalogue(db_session: Session) -> None:
    retailer = _seed_retailer(db_session, "aldi-offers-test")
    connector = AldiOffersConnector(offers=_OFFERS, enabled=True)
    as_of = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)

    result = run_price_sync(db_session, retailer, None, connector, as_of=as_of)

    # A coverage snapshot is written, but the source is honestly declared PARTIAL, never a full
    # catalogue: the honesty guarantee lives in capabilities, and every recorded price is a
    # promotional/loyalty offer (never a full regular-price catalogue).
    assert isinstance(result.coverage, CoverageSnapshot)
    caps = connector.capabilities()
    assert caps.full_catalog is False
    assert caps.partial_catalog is True
    assert caps.prices is False
    recorded_types = {
        row.price_type
        for row in db_session.execute(
            select(PriceObservation).where(PriceObservation.retailer_id == retailer.id)
        )
        .scalars()
        .all()
    }
    assert recorded_types <= {PriceType.PROMOTIONAL.value, PriceType.LOYALTY.value}
    assert "regular" not in recorded_types
