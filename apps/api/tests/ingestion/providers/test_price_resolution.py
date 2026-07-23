"""Price resolution + freshness policy (spec §V/§W) — pure logic, no DB.

Verifies the scope order, that expired/stale/estimated/community are excluded unless the
request allows them, that a material same-scope conflict is reported (never auto-selected),
and the within-scope priority (authorized > confidence > recency).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.services.price_resolution import (
    FreshnessState,
    ObservationView,
    PriceResolutionService,
    ResolutionRequest,
    classify_freshness,
)

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "price_fresh_hours": 24,
        "price_aging_hours": 48,
        "price_expired_hours": 168,
    }
    base.update(over)
    return Settings(**base)


def _obs(amount: str, scope: PriceScope, hours_old: float = 1.0, **over: Any) -> ObservationView:
    kw: dict[str, Any] = {
        "amount": Decimal(amount),
        "price_type": "regular",
        "price_scope": scope,
        "retailer": "dia",
        "source_provider": "parsebot-dia",
        "observed_at": _NOW - timedelta(hours=hours_old),
        "confidence_score": Decimal("1.0"),
        "verification_status": "provider_reported",
    }
    kw.update(over)
    return ObservationView(**kw)


def _svc() -> PriceResolutionService:
    return PriceResolutionService(_settings())


def _req(**over: Any) -> ResolutionRequest:
    return ResolutionRequest(now=_NOW, **over)


# --- freshness ------------------------------------------------------------- #
def test_freshness_bands() -> None:
    s = _settings()
    assert classify_freshness(1, s) is FreshnessState.FRESH
    assert classify_freshness(30, s) is FreshnessState.AGING
    assert classify_freshness(100, s) is FreshnessState.STALE
    assert classify_freshness(200, s) is FreshnessState.EXPIRED


# --- resolution ------------------------------------------------------------ #
def test_empty_is_missing() -> None:
    assert _svc().resolve([], _req()).freshness is FreshnessState.MISSING


def test_exact_store_beats_national() -> None:
    res = _svc().resolve(
        [_obs("1.20", PriceScope.NATIONAL), _obs("1.00", PriceScope.EXACT_STORE)], _req()
    )
    assert res.selected_price == Decimal("1.00")
    assert res.price_scope == "exact_store"
    assert res.fallback_level == 1


def test_expired_is_never_selected() -> None:
    res = _svc().resolve([_obs("1.00", PriceScope.EXACT_STORE, hours_old=200)], _req())
    assert res.selected_price is None
    assert res.resolution_reason == "no_usable_observation"


def test_stale_excluded_unless_allowed() -> None:
    obs = [_obs("1.00", PriceScope.EXACT_STORE, hours_old=100)]  # stale (48<100<168)
    assert _svc().resolve(obs, _req()).selected_price is None
    res = _svc().resolve(obs, _req(allow_stale=True))
    assert res.selected_price == Decimal("1.00")
    assert res.freshness is FreshnessState.STALE


def test_estimated_and_community_gated() -> None:
    est = [_obs("1.00", PriceScope.NATIONAL, price_type="estimated")]
    assert _svc().resolve(est, _req()).selected_price is None
    assert _svc().resolve(est, _req(allow_estimated=True)).selected_price == Decimal("1.00")

    comm = [_obs("1.00", PriceScope.EXACT_STORE, is_community=True)]
    assert _svc().resolve(comm, _req()).selected_price is None
    assert _svc().resolve(comm, _req(allow_community=True)).selected_price == Decimal("1.00")


def test_material_conflict_same_scope_not_auto_selected() -> None:
    res = _svc().resolve(
        [
            _obs("1.00", PriceScope.EXACT_STORE, source_provider="a"),
            _obs("1.50", PriceScope.EXACT_STORE, source_provider="b"),  # +50% -> material
        ],
        _req(),
    )
    assert res.freshness is FreshnessState.CONFLICTING
    assert res.selected_price is None
    assert len(res.alternatives) == 2


def test_within_scope_authorized_wins_then_recency() -> None:
    res = _svc().resolve(
        [
            _obs("1.01", PriceScope.EXACT_STORE, confidence_score=Decimal("1.0"), hours_old=1),
            _obs(
                "1.02",
                PriceScope.EXACT_STORE,
                authorized=True,
                confidence_score=Decimal("0.8"),
                hours_old=2,
            ),
        ],
        _req(),
    )
    # authorized beats a higher-confidence unauthorized observation
    assert res.selected_price == Decimal("1.02")
    assert res.retailer == "dia"
    assert len(res.alternatives) == 1
