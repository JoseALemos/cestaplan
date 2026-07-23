"""FASE 2A final acceptance test (spec §AE) + §AB/§P/§T units — fully synthetic, no network.

Demonstrates the safety promise: a provider failure never produces a basket with fake prices
and never deletes the last valid data. Walks sync -> resolve -> cost -> next-day update ->
history -> missing/expired/conflict -> quarantine on a coverage collapse -> continuity ->
rollback -> recovery.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.providers.comparison import ComparisonState, compare_sources
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ContentUnit,
    ExternalCatalogProduct,
    HealthStatus,
    PriceCatalogProvider,
    ProductQuery,
    ProviderCapabilities,
    ProviderKind,
    ProviderMetadata,
    ProviderStatus,
    SellUnit,
)
from cestaplan_api.models import PriceObservation, ProductVariant, ProviderActivation, Retailer
from cestaplan_api.services.basket_coverage import BasketLine, evaluate_basket_coverage
from cestaplan_api.services.price_resolution import (
    FreshnessState,
    ObservationView,
    PriceResolutionService,
    ResolutionRequest,
)
from cestaplan_api.services.price_rollback import rollback_sync
from cestaplan_api.services.provider_sync import SyncMode, run_provider_sync

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _prod(
    barcode: str, price: str, observed_at: datetime, scope=PriceScope.EXACT_STORE
) -> ExternalCatalogProduct:
    return ExternalCatalogProduct(
        provider="fake",
        retailer_slug="dia",
        external_product_id=barcode,
        product_name=f"Producto {barcode}",
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal(price),
        currency="EUR",
        price_scope=scope,
        observed_at=observed_at,
        availability=Availability.IN_STOCK,
        barcode=barcode,
        net_content_quantity=Decimal("1000"),
        net_content_unit=ContentUnit.ML,
    )


class _FakeProvider(PriceCatalogProvider):
    provider_code = "fake"

    def __init__(self, products: list[ExternalCatalogProduct]) -> None:
        self.products = products

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(full_catalog=True, store_scope=True)

    def get_source_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_code="fake",
            retailer_slug="dia",
            kind=ProviderKind.INDEPENDENT,
            status=ProviderStatus.EXPERIMENTAL,
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(ok=True, detail="ok")

    def iterate_products(self, query: ProductQuery) -> Iterator[ExternalCatalogProduct]:
        yield from self.products


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "price_providers_enabled": True,
        "price_provider_kill_switch": False,
        "provider_require_rights_approval": True,
        "price_fresh_hours": 24,
        "price_aging_hours": 48,
        "price_expired_hours": 168,
        "provider_min_price_coverage": 0.95,
        "provider_min_package_coverage": 0.80,
        "provider_min_observed_at_coverage": 0.95,
        "provider_max_catalog_drop_ratio": 0.50,
    }
    base.update(over)
    return Settings(**base)


def _retailer(db: Session) -> Retailer:
    r = Retailer(slug="e2e-fake-dia", name="DIA (e2e)", adapter_key="fake", is_synthetic=False)
    db.add(r)
    db.flush()
    return r


def _cleared_activation(db: Session, code: str) -> None:
    db.add(
        ProviderActivation(
            provider_code=code,
            transport_status="operational",
            mapper_status="verified",
            data_quality_status="accepted",
            data_rights_status="commercial_use_allowed",
            production_approved_at=_NOW,
            production_approved_by=None,
        )
    )
    # production_approved_by must be non-null per gate; use a real user id
    from cestaplan_api.models import User

    user = User(email="ops@x.com", password_hash="x", display_name="Ops")
    db.add(user)
    db.flush()
    row = db.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == code)
    ).scalar_one()
    row.production_approved_by = user.id
    db.flush()


def _current_views(db: Session, retailer: Retailer, variant_id: int) -> list[ObservationView]:
    rows = db.execute(
        select(PriceObservation).where(
            PriceObservation.product_variant_id == variant_id,
            PriceObservation.valid_until.is_(None),
            PriceObservation.rolled_back_at.is_(None),
            PriceObservation.staging_only.is_(False),
        )
    ).scalars()
    return [
        ObservationView(
            amount=r.amount,
            price_type=r.price_type,
            price_scope=PriceScope(r.price_scope),
            retailer=retailer.slug,
            source_provider="fake",
            observed_at=r.observed_at,
            confidence_score=r.confidence_score,
        )
        for r in rows
    ]


def _variant(db: Session, retailer: Retailer, barcode: str) -> ProductVariant:
    from cestaplan_api.models import ExternalProduct

    return (
        db.execute(
            select(ProductVariant)
            .join(ExternalProduct, ExternalProduct.id == ProductVariant.external_product_id)
            .where(
                ProductVariant.retailer_id == retailer.id, ExternalProduct.external_id == barcode
            )
        )
        .scalars()
        .one()
    )


# --- §AB comparison -------------------------------------------------------- #
def test_comparison_states() -> None:
    a = _prod("8400001", "1.00", _NOW)
    assert (
        compare_sources(a, _prod("8400001", "1.005", _NOW), now=_NOW).state
        is ComparisonState.CONSISTENT
    )
    assert (
        compare_sources(a, _prod("8400001", "1.10", _NOW), now=_NOW).state
        is ComparisonState.MINOR_DIFFERENCE
    )
    assert (
        compare_sources(a, _prod("8400001", "1.50", _NOW), now=_NOW).state
        is ComparisonState.MATERIAL_DIFFERENCE
    )
    assert (
        compare_sources(
            a, _prod("8400001", "1.00", _NOW, scope=PriceScope.NATIONAL), now=_NOW
        ).state
        is ComparisonState.INCOMPATIBLE_SCOPE
    )


# --- §P dry-run ------------------------------------------------------------ #
def test_dry_run_writes_nothing(db_session: Session) -> None:
    retailer = _retailer(db_session)
    provider = _FakeProvider([_prod("8400001", "1.00", _NOW)])
    report = run_provider_sync(
        db_session, provider, retailer, _settings(), mode=SyncMode.DRY_RUN, as_of=_NOW
    )
    assert report.persisted_observations == 0
    assert report.quality_status == "accepted"
    assert (
        db_session.scalar(
            select(PriceObservation.id).where(PriceObservation.retailer_id == retailer.id)
        )
        is None
    )


# --- §AE full acceptance flow ---------------------------------------------- #
def test_full_acceptance_flow(db_session: Session) -> None:
    retailer = _retailer(db_session)
    _cleared_activation(db_session, "fake")
    settings = _settings()

    # (2-4) first production sync inserts products + prices
    day1 = [_prod("8400001", "1.00", _NOW), _prod("8400002", "2.00", _NOW)]
    r1 = run_provider_sync(
        db_session, _FakeProvider(day1), retailer, settings, mode=SyncMode.PRODUCTION, as_of=_NOW
    )
    assert r1.persisted_observations == 2 and r1.run_id is not None

    v1 = _variant(db_session, retailer, "8400001")

    # (6-8) build a basket + cost from resolved current prices
    svc = PriceResolutionService(settings)
    res1 = svc.resolve(_current_views(db_session, retailer, v1.id), ResolutionRequest(now=_NOW))
    assert res1.selected_price == Decimal("1.00")
    line = BasketLine("prod1", res1, has_package_data=True, ingredient_mapped=True)
    assert evaluate_basket_coverage([line]).cost_label == "coste calculado"

    # (9-10) next-day update: new price appends history and closes the prior row
    day2 = _NOW + timedelta(days=1)
    run_provider_sync(
        db_session,
        _FakeProvider([_prod("8400001", "1.20", day2), _prod("8400002", "2.00", day2)]),
        retailer,
        settings,
        mode=SyncMode.PRODUCTION,
        as_of=day2,
    )
    obs = (
        db_session.execute(
            select(PriceObservation)
            .where(PriceObservation.product_variant_id == v1.id)
            .order_by(PriceObservation.valid_from)
        )
        .scalars()
        .all()
    )
    assert len(obs) == 2  # history preserved (append-only)
    assert obs[0].valid_until == day2 and obs[1].valid_until is None
    res2 = svc.resolve(_current_views(db_session, retailer, v1.id), ResolutionRequest(now=day2))
    assert res2.selected_price == Decimal("1.20")

    # (11) product without price -> unresolved line
    empty = svc.resolve([], ResolutionRequest(now=day2))
    assert empty.freshness is FreshnessState.MISSING

    # (12) expired price is never current
    old = [
        ObservationView(
            Decimal("9.99"),
            "regular",
            PriceScope.EXACT_STORE,
            "dia",
            "fake",
            _NOW - timedelta(days=30),
        )
    ]
    assert svc.resolve(old, ResolutionRequest(now=day2)).selected_price is None

    # (13) conflict between two sources at the same scope is not auto-selected
    conflict = svc.resolve(
        [
            ObservationView(Decimal("1.20"), "regular", PriceScope.EXACT_STORE, "dia", "a", day2),
            ObservationView(Decimal("1.90"), "regular", PriceScope.EXACT_STORE, "dia", "b", day2),
        ],
        ResolutionRequest(now=day2),
    )
    assert conflict.freshness is FreshnessState.CONFLICTING and conflict.selected_price is None

    # (14-16) a 90% coverage collapse is quarantined and NEVER replaces the good prices
    bad = run_provider_sync(
        db_session,
        _FakeProvider([_prod("8400001", "0.01", day2 + timedelta(days=1))]),  # 1 vs 2 before
        retailer,
        settings,
        mode=SyncMode.PRODUCTION,
        previous_count=20,
        as_of=day2 + timedelta(days=1),
    )
    assert bad.quarantined is True and bad.persisted_observations == 0
    # continuity: the last good price still resolves (no fake 0.01)
    res3 = svc.resolve(
        _current_views(db_session, retailer, v1.id), ResolutionRequest(now=day2 + timedelta(days=1))
    )
    assert res3.selected_price == Decimal("1.20")

    # (17-18) rollback the day-2 run -> restore the day-1 price; nothing deleted
    day2_run = db_session.execute(
        select(PriceObservation.crawl_run_id, PriceObservation.observed_at)
        .where(PriceObservation.product_variant_id == v1.id)
        .order_by(PriceObservation.valid_from.desc())
    ).first()
    assert day2_run is not None
    from cestaplan_api.models import CrawlRun

    run = db_session.get(CrawlRun, day2_run[0])
    assert run is not None
    rb = rollback_sync(db_session, run.public_id, actor_user_id=None, now=day2 + timedelta(days=2))
    assert rb.reopened == 1 and rb.invalidated == 1
    # history still present (logical rollback, no DELETE)
    assert (
        db_session.scalar(
            select(func.count(PriceObservation.id)).where(
                PriceObservation.product_variant_id == v1.id
            )
        )
        == 2
    )
    # The day-1 (1.00) observation is open again; at day2+2 it is stale, so recovery is
    # confirmed with allow_stale (the value is back, correctly flagged old — never faked).
    res4 = svc.resolve(
        _current_views(db_session, retailer, v1.id),
        ResolutionRequest(now=day2 + timedelta(days=2), allow_stale=True),
    )
    assert res4.selected_price == Decimal("1.00")  # recovered the prior projection
    assert res4.freshness is FreshnessState.STALE

    # idempotent rollback
    rb2 = rollback_sync(db_session, run.public_id, actor_user_id=None, now=day2 + timedelta(days=2))
    assert rb2.already_rolled_back is True
