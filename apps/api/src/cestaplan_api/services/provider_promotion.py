"""Staging → production promotion bridge (spec §P/§O, phase 2).

The staging-first pipeline captures a provider's catalogue into ``staging_only`` price
observations and builds ``ProviderIngredientMapping`` *candidates* that an admin reviews. None of
that is ever used to cost a real plan. This module is the **explicit, audited bridge** that turns
approved staging data into the productive tables the meal-plan engine reads
(``IngredientProductMapping.is_active`` + ``ProductPrice``) — and only after a human has approved
the provider for production.

Two operations, both idempotent and both refusing to write anything unless the gate is clear:

* :func:`approve_provider_production` — the human production-approval action. Sets
  ``production_enabled`` / ``production_approved`` and records the actor + timestamp, but ONLY once
  the transport/mapper/data-quality/rights prerequisites hold. This is the one place those flags
  are ever set to True.
* :func:`promote_provider_to_production` — materializes productive
  :class:`~cestaplan_api.models.catalog.IngredientProductMapping` rows from the approved candidates
  and promotes the chain's ``staging_only`` observations into real ``ProductPrice`` (reusing the
  existing, tested :class:`CurrentPriceService.project_current_prices`). Refuses entirely unless the
  provider is production-approved. Nothing here fabricates a price, a product or a mapping — a
  candidate without a canonical product is simply skipped.

The service mutates the session (flush) but never commits: the caller owns the transaction, so a
``dry_run`` is just "run it, then roll back" and the counts are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.ingestion.providers.activation import (
    _RIGHTS_OK_FOR_PROD,
    evaluate_production,
    get_activation,
)
from cestaplan_api.models import (
    IngredientProductMapping,
    PriceObservation,
    Product,
    ProviderActivation,
    ProviderIngredientMapping,
    Retailer,
)
from cestaplan_api.services.mapping_review import _APPROVED, is_selectable_for_costing

# match_method stamped on productive mappings created by an audited promotion (never a guess).
PROMOTION_MATCH_METHOD = "provider_promotion"


class PromotionBlocked(Exception):
    """A promotion / approval was refused because the production gate is not clear.

    ``reasons`` are typed slugs (``transport_status=degraded``…) safe to surface to an admin — never
    a raw payload or secret.
    """

    def __init__(self, provider_code: str, reasons: list[str]) -> None:
        self.provider_code = provider_code
        self.reasons = reasons
        super().__init__(f"{provider_code} not cleared for production: {', '.join(reasons)}")


@dataclass(slots=True)
class PromotionResult:
    """Outcome of a promotion run. Counts are exact whether or not it was a dry run."""

    provider_code: str
    dry_run: bool
    approved_candidates: int = 0
    mappings_created: int = 0
    observations_promoted: int = 0
    prices_written: int = 0
    retailer_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_code": self.provider_code,
            "dry_run": self.dry_run,
            "approved_candidates": self.approved_candidates,
            "mappings_created": self.mappings_created,
            "observations_promoted": self.observations_promoted,
            "prices_written": self.prices_written,
            "retailer_ids": sorted(self.retailer_ids),
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _production_prerequisite_reasons(
    activation: ProviderActivation, settings: Settings
) -> list[str]:
    """Every production prerequisite EXCEPT the human approval itself (which this gates).

    Mirrors :func:`evaluate_production` but omits the ``not_manually_approved`` clause, so it can be
    used to decide whether a human is *allowed* to grant that approval.
    """
    reasons: list[str] = []
    if settings.price_provider_kill_switch:
        reasons.append("kill_switch_on")
    if not settings.price_providers_enabled:
        reasons.append("price_providers_disabled")
    if activation.transport_status != "operational":
        reasons.append(f"transport_status={activation.transport_status}")
    if activation.mapper_status != "verified":
        reasons.append(f"mapper_status={activation.mapper_status}")
    if activation.data_quality_status != "accepted":
        reasons.append(f"data_quality_status={activation.data_quality_status}")
    if (
        settings.provider_require_rights_approval
        and activation.data_rights_status not in _RIGHTS_OK_FOR_PROD
    ):
        reasons.append(f"data_rights_status={activation.data_rights_status}")
    return reasons


def approve_provider_production(
    db: Session, *, provider_code: str, actor_id: int, settings: Settings | None = None
) -> ProviderActivation:
    """Grant production approval for a provider, recording the actor + timestamp.

    Refuses (``PromotionBlocked``) unless every non-approval prerequisite holds. This is the sole
    place ``production_enabled`` / ``production_approved`` are set True. Idempotent: re-approving an
    already-approved provider keeps the original ``production_approved_by``/``_at``.
    """
    settings = settings or get_settings()
    activation = get_activation(db, provider_code)
    if activation is None:
        raise PromotionBlocked(provider_code, ["no_activation_record"])

    reasons = _production_prerequisite_reasons(activation, settings)
    if reasons:
        raise PromotionBlocked(provider_code, reasons)

    activation.production_enabled = True
    activation.production_approved = True
    if activation.production_approved_at is None:
        activation.production_approved_at = _now()
    if activation.production_approved_by is None:
        activation.production_approved_by = actor_id
    activation.activation_state = "production_primary"
    db.flush()
    return activation


def _production_gate_reasons(db: Session, provider_code: str, settings: Settings) -> list[str]:
    """Reasons a promotion must refuse: the full production gate PLUS the boolean flags."""
    decision = evaluate_production(db, provider_code, settings)
    reasons = list(decision.reasons)
    activation = get_activation(db, provider_code)
    if activation is not None and not (
        activation.production_enabled and activation.production_approved
    ):
        reasons.append("production_flags_not_set")
    return reasons


def _approved_candidates(db: Session, provider_code: str) -> list[ProviderIngredientMapping]:
    rows = (
        db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.provider_code == provider_code,
                ProviderIngredientMapping.mapping_status.in_(_APPROVED),
            )
        )
        .scalars()
        .all()
    )
    # Only approved + active + resolvable-to-a-canonical-product candidates can be promoted.
    return [
        row
        for row in rows
        if is_selectable_for_costing(row) and row.normalized_product_id is not None
    ]


def _candidate_retailer_ids(
    db: Session, candidates: list[ProviderIngredientMapping]
) -> set[int]:
    """Resolve the distinct retailer ids of a set of candidates from their (authoritative) slug."""
    slugs = {c.retailer_slug for c in candidates}
    if not slugs:
        return set()
    rows = db.execute(
        select(Retailer.id).where(Retailer.slug.in_(slugs))
    ).scalars().all()
    return set(rows)


def promote_provider_to_production(
    db: Session,
    *,
    provider_code: str,
    actor_id: int,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> PromotionResult:
    """Materialize productive mappings + prices from a provider's approved staging data.

    Refuses (``PromotionBlocked``) unless the provider is production-approved. Idempotent: an
    ingredient/product mapping already present is not duplicated, and an already-promoted
    observation is not re-counted. The caller owns the transaction — commit for a real run, roll
    back for a preview; the returned counts are exact either way.
    """
    settings = settings or get_settings()
    reasons = _production_gate_reasons(db, provider_code, settings)
    if reasons:
        raise PromotionBlocked(provider_code, reasons)

    candidates = _approved_candidates(db, provider_code)
    result = PromotionResult(
        provider_code=provider_code, dry_run=dry_run, approved_candidates=len(candidates)
    )

    now = _now()
    retailer_ids: set[int] = set()
    slug_to_retailer_id: dict[str, int | None] = {}
    for candidate in candidates:
        product = db.get(Product, candidate.normalized_product_id)
        if product is None:
            continue
        # The candidate's retailer_slug is the authoritative chain (Product.retailer_id may be
        # unset on legacy rows); a plan is always priced against one chain.
        if candidate.retailer_slug not in slug_to_retailer_id:
            retailer = db.execute(
                select(Retailer).where(Retailer.slug == candidate.retailer_slug)
            ).scalars().first()
            slug_to_retailer_id[candidate.retailer_slug] = retailer.id if retailer else None
        retailer_id = slug_to_retailer_id[candidate.retailer_slug]
        if retailer_id is None:
            continue
        retailer_ids.add(retailer_id)
        already = db.execute(
            select(IngredientProductMapping.id).where(
                IngredientProductMapping.ingredient_id == candidate.ingredient_id,
                IngredientProductMapping.product_id == product.id,
            )
        ).first()
        if already is not None:
            continue
        db.add(
            IngredientProductMapping(
                ingredient_id=candidate.ingredient_id,
                product_id=product.id,
                retailer_id=retailer_id,
                confidence_score=candidate.confidence_score,
                match_method=PROMOTION_MATCH_METHOD,
                # Human-approved audited promotion (the enum has no bare "verified").
                verification_status="human_verified",
                verified_by=actor_id,
                verified_at=now,
                is_active=True,
            )
        )
        result.mappings_created += 1

    # Promote the chain's staged observations into production, then reuse the existing projector
    # to write ProductPrice from the newest valid (now non-staging) observation per variant.
    price_service = CurrentPriceService()
    for retailer_id in sorted(retailer_ids):
        staged = (
            db.execute(
                select(PriceObservation).where(
                    PriceObservation.retailer_id == retailer_id,
                    PriceObservation.staging_only.is_(True),
                    PriceObservation.rolled_back_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for obs in staged:
            obs.staging_only = False
        result.observations_promoted += len(staged)
        if staged:
            db.flush()
            result.prices_written += price_service.project_current_prices(db, retailer_id)

    result.retailer_ids = sorted(retailer_ids)
    db.flush()
    return result


def promotion_status(
    db: Session, *, provider_code: str, settings: Settings | None = None
) -> dict[str, object]:
    """Read-only summary for the admin panel: gate reasons + what a promotion would touch."""
    settings = settings or get_settings()
    gate_reasons = _production_gate_reasons(db, provider_code, settings)
    candidates = _approved_candidates(db, provider_code)
    # Only the provider's own chain(s) — never a global count that would fold in other providers.
    retailer_ids = _candidate_retailer_ids(db, candidates)
    staged_observations = 0
    if retailer_ids:
        staged_observations = int(
            db.scalar(
                select(func.count())
                .select_from(PriceObservation)
                .where(
                    PriceObservation.retailer_id.in_(retailer_ids),
                    PriceObservation.staging_only.is_(True),
                    PriceObservation.rolled_back_at.is_(None),
                )
            )
            or 0
        )
    return {
        "provider_code": provider_code,
        "production_ready": not gate_reasons,
        "gate_reasons": gate_reasons,
        "approved_candidates": len(candidates),
        "staged_observations": staged_observations,
    }


__all__ = [
    "PromotionBlocked",
    "PromotionResult",
    "approve_provider_production",
    "promote_provider_to_production",
    "promotion_status",
]
