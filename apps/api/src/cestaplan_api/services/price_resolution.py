"""Price selection + freshness policy (spec §V/§W).

``PriceResolutionService`` picks, from many observations of one product, the single price to
use — by a configured scope order, then within a scope by usability and confidence. It is
independent of any provider and never silently mixes stores or scopes: a material conflict
inside the winning scope is reported, not auto-resolved. Freshness bands (§W) come from
:class:`~cestaplan_api.config.Settings`, never hard-coded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.contracts import PriceScope

# Material relative gap (0..1) above which two same-scope observations are a conflict.
_MATERIAL_DIFF = Decimal("0.15")

_SCOPE_RANK: dict[PriceScope, int] = {
    PriceScope.EXACT_STORE: 1,
    PriceScope.DELIVERY_ZONE: 2,
    PriceScope.POSTAL_CODE: 3,
    PriceScope.MUNICIPALITY: 4,
    PriceScope.PROVINCE: 5,
    PriceScope.REGION: 6,
    PriceScope.NATIONAL: 7,
}
_VERIFICATION_RANK: dict[str, int] = {
    "manually_verified": 3,
    "automatically_validated": 2,
    "provider_reported": 1,
}


class FreshnessState(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    EXPIRED = "expired"
    MISSING = "missing"
    CONFLICTING = "conflicting"
    QUARANTINED = "quarantined"


def classify_freshness(age_hours: float, settings: Settings) -> FreshnessState:
    """Map an observation age (hours) to a freshness band using configured thresholds."""
    if age_hours < settings.price_fresh_hours:
        return FreshnessState.FRESH
    if age_hours < settings.price_aging_hours:
        return FreshnessState.AGING
    if age_hours < settings.price_expired_hours:
        return FreshnessState.STALE
    return FreshnessState.EXPIRED


@dataclass(slots=True)
class ObservationView:
    """A provider-independent price observation candidate for resolution."""

    amount: Decimal
    price_type: str  # PriceType value, or "manual" / "estimated"
    price_scope: PriceScope
    retailer: str
    source_provider: str
    observed_at: datetime
    confidence_score: Decimal = Decimal("1.0")
    verification_status: str = "provider_reported"
    store_id: str | None = None
    authorized: bool = False  # authorized / commercial-approved source
    verifiable_evidence: bool = False
    is_community: bool = False
    quarantined: bool = False


@dataclass(slots=True)
class ResolutionRequest:
    now: datetime
    allow_stale: bool = False
    allow_estimated: bool = False
    allow_community: bool = False


@dataclass(slots=True)
class PriceResolution:
    selected_price: Decimal | None = None
    price_type: str | None = None
    price_scope: str | None = None
    retailer: str | None = None
    store_id: str | None = None
    source_provider: str | None = None
    observed_at: datetime | None = None
    age_hours: float | None = None
    confidence_score: Decimal | None = None
    verification_status: str | None = None
    freshness: FreshnessState = FreshnessState.MISSING
    fallback_level: int | None = None
    resolution_reason: str = "no_observation"
    alternatives: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "selected_price": str(self.selected_price) if self.selected_price is not None else None,
            "price_type": self.price_type,
            "price_scope": self.price_scope,
            "retailer": self.retailer,
            "store_id": self.store_id,
            "source_provider": self.source_provider,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "age_hours": self.age_hours,
            "confidence_score": str(self.confidence_score)
            if self.confidence_score is not None
            else None,
            "verification_status": self.verification_status,
            "freshness": self.freshness.value,
            "fallback_level": self.fallback_level,
            "resolution_reason": self.resolution_reason,
            "alternatives": self.alternatives,
            "warnings": self.warnings,
        }


def _age_hours(obs: ObservationView, now: datetime) -> float:
    return max(0.0, (now - obs.observed_at).total_seconds() / 3600.0)


def _level(obs: ObservationView) -> int:
    if obs.price_type == "estimated":
        return 10
    if obs.price_type == "manual":
        return 9
    if obs.is_community:
        return 8
    return _SCOPE_RANK.get(obs.price_scope, 11)


class PriceResolutionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(
        self, observations: list[ObservationView], request: ResolutionRequest
    ) -> PriceResolution:
        """Select one price from ``observations`` per the scope + usability policy."""
        if not observations:
            return PriceResolution()

        usable: list[ObservationView] = []
        for obs in observations:
            if obs.quarantined:  # never used
                continue
            freshness = classify_freshness(_age_hours(obs, request.now), self._settings)
            if freshness is FreshnessState.EXPIRED:  # never a current price
                continue
            if freshness is FreshnessState.STALE and not request.allow_stale:
                continue
            if obs.price_type == "estimated" and not request.allow_estimated:
                continue
            if obs.is_community and not request.allow_community:
                continue
            usable.append(obs)

        if not usable:
            res = PriceResolution(resolution_reason="no_usable_observation")
            res.warnings.append("all observations expired/quarantined or disallowed by policy")
            return res

        # Best (lowest) fallback level with any candidate — never mix scopes.
        best_level = min(_level(o) for o in usable)
        tier = [o for o in usable if _level(o) == best_level]

        conflict = self._material_conflict(tier)
        if conflict:
            res = PriceResolution(freshness=FreshnessState.CONFLICTING, fallback_level=best_level)
            res.resolution_reason = "conflicting_sources_same_scope"
            res.warnings.append("material price conflict in the winning scope; not auto-selected")
            res.alternatives = [self._alt(o, request.now) for o in tier]
            return res

        chosen = self._prioritize(tier)
        return self._build(chosen, tier, best_level, request)

    def _material_conflict(self, tier: list[ObservationView]) -> bool:
        if len(tier) < 2:
            return False
        amounts = [o.amount for o in tier]
        low, high = min(amounts), max(amounts)
        if low <= 0:
            return high > 0
        return (high - low) / low > _MATERIAL_DIFF

    def _prioritize(self, tier: list[ObservationView]) -> ObservationView:
        # not-expired (already ensured) -> authorized -> confidence -> evidence -> recency.
        return sorted(
            tier,
            key=lambda o: (
                o.authorized,
                o.confidence_score,
                _VERIFICATION_RANK.get(o.verification_status, 0),
                o.verifiable_evidence,
                o.observed_at,
            ),
            reverse=True,
        )[0]

    def _build(
        self,
        chosen: ObservationView,
        tier: list[ObservationView],
        level: int,
        request: ResolutionRequest,
    ) -> PriceResolution:
        age = _age_hours(chosen, request.now)
        freshness = classify_freshness(age, self._settings)
        res = PriceResolution(
            selected_price=chosen.amount,
            price_type=chosen.price_type,
            price_scope=chosen.price_scope.value,
            retailer=chosen.retailer,
            store_id=chosen.store_id,
            source_provider=chosen.source_provider,
            observed_at=chosen.observed_at,
            age_hours=round(age, 2),
            confidence_score=chosen.confidence_score,
            verification_status=chosen.verification_status,
            freshness=freshness,
            fallback_level=level,
            resolution_reason=f"selected at scope level {level}",
            alternatives=[self._alt(o, request.now) for o in tier if o is not chosen],
        )
        if freshness is FreshnessState.AGING:
            res.warnings.append("price is aging (>fresh threshold)")
        if freshness is FreshnessState.STALE:
            res.warnings.append("stale price used (allowed by request)")
        return res

    def _alt(self, obs: ObservationView, now: datetime) -> dict[str, object]:
        return {
            "amount": str(obs.amount),
            "price_scope": obs.price_scope.value,
            "retailer": obs.retailer,
            "source_provider": obs.source_provider,
            "age_hours": round(_age_hours(obs, now), 2),
            "confidence_score": str(obs.confidence_score),
        }


__all__ = [
    "FreshnessState",
    "ObservationView",
    "PriceResolution",
    "PriceResolutionService",
    "ResolutionRequest",
    "classify_freshness",
]
