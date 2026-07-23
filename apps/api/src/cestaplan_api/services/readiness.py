"""Licensed-catalog readiness gate (FASE 5).

Before a real chain can replace the demo catalogue, it must pass an auditable gate covering
the eight exit criteria. :func:`evaluate_readiness` measures them per retailer against the
DB (plus an operator-attested license flag, since a signed contract cannot be verified
programmatically) and returns a :class:`ReadinessReport`. ``can_retire_demo`` is True only
when every criterion passes — the demo stays until then.

Criteria:
1. license_verified          - operator attests the licence is signed (cannot be auto-checked)
2. min_coverage              - verified-ingredient coverage >= the agreed minimum
3. field_mapping_validated   - a supplier field map covering the required targets exists
4. incremental_update        - at least one variant has >1 observation (a sync appended)
5. idempotency               - no (variant, scope, store) has more than one open observation
6. price_history             - at least one observation has been closed (valid_until set)
7. ingredient_coverage       - verified-ingredient coverage has been measured (> 0)
8. no_critical_errors        - no non-positive money and no active mapping on a non-costable unit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.licensed_catalog import COSTABLE_UNITS
from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    PriceObservation,
    ProductVariant,
    Retailer,
    SupplierFieldMapping,
)

_REQUIRED_TARGET_FIELDS = ("external_id", "product_name", "amount")


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Tunable inputs to the gate: the agreed coverage floor and the licence attestation."""

    min_ingredient_coverage: float = 0.60
    license_verified: bool = False


@dataclass(slots=True)
class CriterionResult:
    key: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class ReadinessReport:
    retailer: str
    retailer_id: str
    can_retire_demo: bool
    ingredient_coverage_ratio: float
    criteria: list[CriterionResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "retailer": self.retailer,
            "retailer_id": self.retailer_id,
            "can_retire_demo": self.can_retire_demo,
            "ingredient_coverage_ratio": self.ingredient_coverage_ratio,
            "criteria": [c.as_dict() for c in self.criteria],
        }


def _verified_ingredient_count(db: Session, retailer_id: int) -> int:
    return (
        db.scalar(
            select(func.count(func.distinct(IngredientProductMapping.ingredient_id))).where(
                IngredientProductMapping.retailer_id == retailer_id,
                IngredientProductMapping.is_active.is_(True),
                IngredientProductMapping.verification_status == "human_verified",
            )
        )
        or 0
    )


def _has_valid_field_mapping(db: Session) -> bool:
    for mapping in db.execute(
        select(SupplierFieldMapping).where(SupplierFieldMapping.is_active.is_(True))
    ).scalars():
        if all(f in mapping.field_map for f in _REQUIRED_TARGET_FIELDS):
            return True
    return False


def _max_observations_per_variant(db: Session, retailer_id: int) -> int:
    counts = (
        select(func.count(PriceObservation.id).label("n"))
        .join(ProductVariant, ProductVariant.id == PriceObservation.product_variant_id)
        .where(ProductVariant.retailer_id == retailer_id)
        .group_by(PriceObservation.product_variant_id)
        .subquery()
    )
    return db.scalar(select(func.coalesce(func.max(counts.c.n), 0))) or 0


def _open_observation_conflicts(db: Session, retailer_id: int) -> int:
    """Count (variant, scope, store) groups with >1 open observation — an idempotency break."""
    grouped = (
        select(func.count(PriceObservation.id).label("n"))
        .where(
            PriceObservation.retailer_id == retailer_id,
            PriceObservation.valid_until.is_(None),
        )
        .group_by(
            PriceObservation.product_variant_id,
            PriceObservation.price_scope,
            PriceObservation.store_id,
        )
        .having(func.count(PriceObservation.id) > 1)
        .subquery()
    )
    return db.scalar(select(func.count()).select_from(grouped)) or 0


def _has_closed_observation(db: Session, retailer_id: int) -> bool:
    return (
        db.scalar(
            select(func.count(PriceObservation.id)).where(
                PriceObservation.retailer_id == retailer_id,
                PriceObservation.valid_until.is_not(None),
            )
        )
        or 0
    ) > 0


def _money_errors(db: Session, retailer_id: int) -> int:
    return (
        db.scalar(
            select(func.count(PriceObservation.id)).where(
                PriceObservation.retailer_id == retailer_id,
                PriceObservation.amount <= 0,
            )
        )
        or 0
    )


def _unit_errors(db: Session, retailer_id: int) -> int:
    """Active mappings whose variant has no costable net-content unit (would misprice)."""
    return (
        db.scalar(
            select(func.count(IngredientProductMapping.id))
            .join(
                ProductVariant,
                ProductVariant.id == IngredientProductMapping.product_variant_id,
            )
            .where(
                IngredientProductMapping.retailer_id == retailer_id,
                IngredientProductMapping.is_active.is_(True),
                func.lower(func.coalesce(ProductVariant.net_content_unit, "")).notin_(
                    COSTABLE_UNITS
                ),
            )
        )
        or 0
    )


def evaluate_readiness(
    db: Session, retailer: Retailer, config: GateConfig | None = None
) -> ReadinessReport:
    """Measure the eight exit criteria for ``retailer`` and whether the demo can be retired."""
    config = config or GateConfig()
    total_ingredients = db.scalar(select(func.count(Ingredient.id))) or 0
    verified = _verified_ingredient_count(db, retailer.id)
    coverage = round(verified / total_ingredients, 4) if total_ingredients else 0.0

    money_errors = _money_errors(db, retailer.id)
    unit_errors = _unit_errors(db, retailer.id)
    conflicts = _open_observation_conflicts(db, retailer.id)

    criteria = [
        CriterionResult(
            "license_verified",
            config.license_verified,
            "operator attestation" if config.license_verified else "licence not attested",
        ),
        CriterionResult(
            "min_coverage",
            coverage >= config.min_ingredient_coverage,
            f"coverage {coverage} vs floor {config.min_ingredient_coverage}",
        ),
        CriterionResult(
            "field_mapping_validated",
            _has_valid_field_mapping(db),
            "a supplier field map covers the required targets",
        ),
        CriterionResult(
            "incremental_update",
            _max_observations_per_variant(db, retailer.id) > 1,
            "at least one variant has more than one observation",
        ),
        CriterionResult(
            "idempotency",
            conflicts == 0,
            f"{conflicts} variant(s) with duplicate open observations",
        ),
        CriterionResult(
            "price_history",
            _has_closed_observation(db, retailer.id),
            "at least one observation has been closed (valid_until set)",
        ),
        CriterionResult(
            "ingredient_coverage",
            verified > 0,
            f"{verified}/{total_ingredients} ingredients human-verified",
        ),
        CriterionResult(
            "no_critical_errors",
            money_errors == 0 and unit_errors == 0,
            f"{money_errors} money error(s), {unit_errors} unit error(s)",
        ),
    ]
    return ReadinessReport(
        retailer=retailer.name,
        retailer_id=str(retailer.public_id),
        can_retire_demo=all(c.passed for c in criteria),
        ingredient_coverage_ratio=coverage,
        criteria=criteria,
    )


__all__ = ["CriterionResult", "GateConfig", "ReadinessReport", "evaluate_readiness"]
