"""Production-readiness candidacy for a provider (spec §AC criteria) — reports, never activates.

Computes whether a provider COULD be a candidate for ``production_partial`` against a set of
configurable minimums. It NEVER changes ``activation_state`` or approves anything — a human still
has to act. Every criterion is reported with its measured value so the gap is explicit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import (
    PriceAnomaly,
    PriceObservation,
    ProviderActivation,
    ProviderIngredientMapping,
    Retailer,
    ShadowEvaluationRun,
)
from cestaplan_api.services.mapping_review import candidate_metrics
from cestaplan_api.services.recipe_catalog_coverage import evaluate_recipe_catalog_coverage

_APPROVED_RIGHTS = {"commercial_use_allowed", "display_allowed", "storage_allowed"}


@dataclass(slots=True)
class ProductionReadinessReport:
    provider_code: str
    retailer_slug: str
    evaluated_at: str
    # Individual criteria (all must hold for candidacy).
    rights_approved: bool = False
    mapper_verified: bool = False
    fingerprint_known: bool = False
    no_critical_anomalies: bool = False
    price_coverage_sufficient: bool = False
    package_coverage_sufficient: bool = False
    recipe_coverage_sufficient: bool = False
    consecutive_successful_syncs_ok: bool = False
    acceptable_age: bool = False
    rollback_tested: bool = False
    shadow_runs_ok: bool = False
    # Measured values behind the checks.
    price_coverage: str = "0"
    package_coverage: str = "0"
    recipe_costing_coverage: str = "0"
    shadow_runs_completed: int = 0
    open_critical_anomalies: int = 0
    # Outcome — CANDIDACY ONLY, never an activation.
    candidate_for_production_partial: bool = False
    would_change_state: bool = False  # always False: this report never mutates state
    blocking_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_production_readiness(
    db: Session,
    provider_code: str,
    *,
    min_price_coverage: Decimal = Decimal("0.8"),
    min_package_coverage: Decimal = Decimal("0.8"),
    min_recipe_coverage: Decimal = Decimal("0.6"),
    min_consecutive_syncs: int = 3,
    min_shadow_runs: int = 1,
    max_price_age_hours: int = 48,
    rollback_tested: bool = False,
    now: datetime | None = None,
) -> ProductionReadinessReport:
    """Report whether ``provider_code`` is a candidate for production_partial. Never activates."""
    now = now or datetime.now(UTC)
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    report = ProductionReadinessReport(
        provider_code=provider_code, retailer_slug=retailer_slug, evaluated_at=now.isoformat()
    )
    activation = db.execute(
        select(ProviderActivation).where(ProviderActivation.provider_code == provider_code)
    ).scalar_one_or_none()
    if activation is None:
        report.blocking_reasons.append("sin registro de activación")
        return report

    report.rights_approved = activation.data_rights_status in _APPROVED_RIGHTS
    report.mapper_verified = activation.mapper_status == "verified"
    report.fingerprint_known = activation.mapper_status in ("verified", "pending")
    report.price_coverage = str(activation.price_coverage or Decimal("0"))
    report.package_coverage = str(activation.costing_eligible_product_coverage or Decimal("0"))
    report.price_coverage_sufficient = (
        activation.price_coverage or Decimal("0")
    ) >= min_price_coverage
    report.package_coverage_sufficient = (
        activation.costing_eligible_product_coverage or Decimal("0")
    ) >= min_package_coverage

    coverage = evaluate_recipe_catalog_coverage(db, provider_code, scope="staging", now=now)
    report.recipe_costing_coverage = str(coverage.costing_coverage)
    report.recipe_coverage_sufficient = coverage.costing_coverage >= min_recipe_coverage

    report.open_critical_anomalies = _open_critical_anomalies(db, retailer_slug)
    report.no_critical_anomalies = report.open_critical_anomalies == 0

    shadow_runs = list(
        db.execute(
            select(ShadowEvaluationRun).where(
                ShadowEvaluationRun.provider_code == provider_code,
                ShadowEvaluationRun.status == "completed",
            )
        ).scalars()
    )
    report.shadow_runs_completed = len(shadow_runs)
    report.shadow_runs_ok = len(shadow_runs) >= min_shadow_runs
    report.consecutive_successful_syncs_ok = len(shadow_runs) >= min_consecutive_syncs
    latest = max((r.completed_at for r in shadow_runs if r.completed_at), default=None)
    report.acceptable_age = latest is not None and (now - latest) <= timedelta(
        hours=max_price_age_hours
    )
    report.rollback_tested = rollback_tested
    # Pending mapping review: any candidate mapping still awaiting a human decision blocks.
    pending_review = int(
        db.execute(
            select(func.count(ProviderIngredientMapping.id)).where(
                ProviderIngredientMapping.provider_code == provider_code,
                ProviderIngredientMapping.required_review.is_(True),
                ProviderIngredientMapping.reviewed_at.is_(None),
            )
        ).scalar_one()
    )
    mapping_review_ok = pending_review == 0
    # Differentiated scope blockers (spec §2). ``partial_catalog_only`` is reserved for chains
    # whose INTENDED role is partial (Lidl/Aldi/Deza). A dense_candidate on a small sample is
    # blocked by sample_only_coverage / insufficient_observed_catalog_coverage instead.
    intended_partial = (entry.intended_catalog_scope == "partial") if entry else False
    observed = activation.observed_catalog_scope
    geo_ok = (activation.geographic_scope_coverage or Decimal("0")) >= Decimal("0.5")
    schema_drift_ok = activation.mapper_status == "verified"
    # A critical candidate explosion (many products claimed by many ingredients) blocks and means
    # nothing may auto-approve until reviewed (§2).
    explosion_ok = bool(candidate_metrics(db, provider_code).get("auto_approval_allowed", True))

    checks: dict[str, bool] = {
        "candidate_explosion_critical": explosion_ok,
        "rights_not_approved": report.rights_approved,
        "mapper_not_verified": report.mapper_verified,
        "fingerprint_unknown": report.fingerprint_known,
        "open_critical_anomalies": report.no_critical_anomalies,
        "insufficient_price_coverage": report.price_coverage_sufficient,
        "insufficient_package_coverage": report.package_coverage_sufficient,
        "insufficient_recipe_coverage": report.recipe_coverage_sufficient,
        "insufficient_mapping_review": mapping_review_ok,
        "insufficient_consecutive_syncs": report.consecutive_successful_syncs_ok,
        "unacceptable_age": report.acceptable_age,
        "rollback_not_proven": report.rollback_tested,
        "insufficient_shadow_runs": report.shadow_runs_ok,
        "unresolved_geographic_scope": geo_ok,
        "schema_drift_risk": schema_drift_ok,
    }
    if intended_partial:
        # Partial chains are catalogue-limited by design.
        checks["partial_catalog_only"] = observed in ("partial", "full")
    else:
        # Dense candidates: a sample never proves catalogue breadth.
        checks["sample_only_coverage"] = observed != "sample_only"
        checks["insufficient_observed_catalog_coverage"] = observed in ("partial", "full")
    report.blocking_reasons = [reason for reason, ok in checks.items() if not ok]
    report.candidate_for_production_partial = not report.blocking_reasons
    return report


def _open_critical_anomalies(db: Session, retailer_slug: str) -> int:
    rid = db.execute(select(Retailer.id).where(Retailer.slug == retailer_slug)).scalar_one_or_none()
    if rid is None:
        return 0
    return int(
        db.execute(
            select(func.count(PriceAnomaly.id))
            .join(PriceObservation, PriceObservation.id == PriceAnomaly.price_observation_id)
            .where(
                PriceObservation.retailer_id == rid,
                PriceAnomaly.severity == "critical",
                PriceAnomaly.status == "open",
            )
        ).scalar_one()
    )


__all__ = ["ProductionReadinessReport", "evaluate_production_readiness"]
