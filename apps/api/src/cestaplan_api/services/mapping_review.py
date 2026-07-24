"""Admin review + dedup for ProviderIngredientMapping (spec §1/§3/§4/§5/§10).

Decisions are auditable and never physically deleted: dedup marks redundant rows superseded,
rejects stay as history, revokes deactivate without erasing. Nothing here ever changes
production_eligibility. ``recipes_potentially_unlocked`` ranks candidates by real impact — how
many recipes an approval would make fully costable — not by isolated similarity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService, FreshnessStatus
from cestaplan_api.ingestion.providers.contracts import ProductCostingMode
from cestaplan_api.ingestion.providers.onboarding import (
    classify_variant_costing_mode,
    get_entry,
)
from cestaplan_api.models import (
    IngredientProductMapping,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    Retailer,
)

MAPPING_VERSION = "1.0.0"


class ReviewError(Exception):
    """A review action was refused (bad state, ambiguous bulk, unknown id)."""


# --------------------------------------------------------------------------- #
# Audit + dedup (§1)
# --------------------------------------------------------------------------- #
def audit(db: Session, provider_code: str | None = None) -> dict[str, object]:
    stmt = select(ProviderIngredientMapping)
    if provider_code:
        stmt = stmt.where(ProviderIngredientMapping.provider_code == provider_code)
    rows = list(db.execute(stmt).scalars())
    active = [r for r in rows if r.superseded_at is None]
    prod_to_ings: dict[tuple[str, str], set[int]] = defaultdict(set)
    for r in active:
        prod_to_ings[(r.provider_code, r.external_product_id)].add(r.ingredient_id)
    return {
        "total": len(rows),
        "active_non_superseded": len(active),
        "unique_external_products": len({r.external_product_id for r in rows}),
        "unique_ingredients": len({r.ingredient_id for r in rows}),
        "products_mapped_to_multiple_ingredients": sum(
            1 for s in prod_to_ings.values() if len(s) > 1
        ),
        "by_status": dict(Counter(r.mapping_status for r in rows)),
        "by_required_review": {
            str(k): v for k, v in Counter(r.required_review for r in rows).items()
        },
        "superseded": sum(1 for r in rows if r.superseded_at is not None),
    }


def consolidate_duplicates(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """A product mapped to several ingredients keeps only its best-confidence row; the rest are
    marked superseded (kept for audit, deactivated). Idempotent."""
    now = now or datetime.now(UTC)
    rows = list(
        db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.superseded_at.is_(None)
            )
        ).scalars()
    )
    groups: dict[tuple[str, str], list[ProviderIngredientMapping]] = defaultdict(list)
    for r in rows:
        groups[(r.provider_code, r.external_product_id)].append(r)
    superseded = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        winner = max(group, key=lambda m: (float(m.confidence_score or 0), m.active, -m.id))
        for m in group:
            if m.id == winner.id:
                continue
            m.superseded_at = now
            m.superseded_reason = (
                f"product better mapped to ingredient {winner.ingredient_id} "
                f"(conf {winner.confidence_score})"
            )
            m.active = False
            superseded += 1
    db.flush()
    return {"groups": len(groups), "superseded": superseded}


# --------------------------------------------------------------------------- #
# Impact ranking (§4/§8)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Impact:
    costable_ingredient_ids: set[int]
    unlock_map: dict[int, int]  # ingredient_id -> recipes it ALONE currently blocks


def _costable_ingredient_ids(db: Session, provider_code: str, now: datetime) -> set[int]:
    """Ingredient ids costable for a provider from active mappings + staged priced variants."""
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()
    if retailer_id is None:
        return set()
    ing_products: dict[int, list[int]] = defaultdict(list)
    for ing_id, prod_id in db.execute(
        select(IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id).where(
            IngredientProductMapping.retailer_id == retailer_id,
            IngredientProductMapping.is_active.is_(True),
        )
    ).all():
        if ing_id is not None and prod_id is not None:
            ing_products[ing_id].append(prod_id)
    for ing_id, prod_id in db.execute(
        select(
            ProviderIngredientMapping.ingredient_id,
            ProviderIngredientMapping.normalized_product_id,
        ).where(
            ProviderIngredientMapping.provider_code == provider_code,
            ProviderIngredientMapping.active.is_(True),
            ProviderIngredientMapping.normalized_product_id.is_not(None),
        )
    ).all():
        if ing_id is not None and prod_id is not None:
            ing_products[ing_id].append(prod_id)
    variants: dict[int, list[ProductVariant]] = defaultdict(list)
    for v in db.execute(
        select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id, ProductVariant.active.is_(True)
        )
    ).scalars():
        if v.product_id is not None:
            variants[v.product_id].append(v)
    prices = CurrentPriceService()
    costable: set[int] = set()
    for ing_id, product_ids in ing_products.items():
        for product_id in product_ids:
            ok = False
            for v in variants.get(product_id, []):
                price = prices.current(db, v.id, as_of=now, staging=True)
                if price is None or price.status is not FreshnessStatus.FRESH:
                    continue
                mode = classify_variant_costing_mode(
                    sell_unit=v.sell_unit,
                    variable_weight=v.variable_weight,
                    net_content_quantity=v.net_content_quantity,
                    net_content_unit=v.net_content_unit,
                    unit_price=v.unit_price,
                    unit_price_unit=v.unit_price_unit,
                    has_price=True,
                )
                if mode is not ProductCostingMode.UNRESOLVED:
                    ok = True
                    break
            if ok:
                costable.add(ing_id)
                break
    return costable


def _impact(db: Session, provider_code: str, now: datetime, *, recipe_limit: int = 20) -> _Impact:
    costable = _costable_ingredient_ids(db, provider_code, now)
    recipes = list(
        db.execute(
            select(Recipe)
            .where(Recipe.deleted_at.is_(None), Recipe.is_synthetic.is_(True))
            .order_by(Recipe.id)
            .limit(recipe_limit)
        ).scalars()
    )
    unlock: Counter[int] = Counter()
    for recipe in recipes:
        blocking = {
            ri.ingredient_id
            for ri in recipe.ingredients
            if not ri.optional and ri.ingredient_id not in costable
        }
        if len(blocking) == 1:  # a single remaining blocker -> approving it unlocks the recipe
            unlock[next(iter(blocking))] += 1
    return _Impact(costable, dict(unlock))


def recipes_potentially_unlocked(
    db: Session, provider_code: str, ingredient_id: int, now: datetime | None = None
) -> int:
    return _impact(db, provider_code, now or datetime.now(UTC)).unlock_map.get(ingredient_id, 0)


# --------------------------------------------------------------------------- #
# Decisions (§5/§10)
# --------------------------------------------------------------------------- #
def _get(db: Session, mapping_id: int) -> ProviderIngredientMapping:
    row = db.get(ProviderIngredientMapping, mapping_id)
    if row is None:
        raise ReviewError(f"mapping {mapping_id} not found")
    return row


def approve(
    db: Session,
    mapping_id: int,
    *,
    reviewer_id: int,
    reason: str | None,
    now: datetime | None = None,
) -> ProviderIngredientMapping:
    now = now or datetime.now(UTC)
    row = _get(db, mapping_id)
    row.mapping_status = "manually_approved"
    row.required_review = False
    row.active = True
    row.reviewed_at = now
    row.reviewed_by = reviewer_id
    row.review_reason = reason
    row.evidence_json = {
        **(row.evidence_json or {}),
        "decision": "manually_approved",
        "reason": reason,
    }
    db.flush()
    return row


def reject(
    db: Session, mapping_id: int, *, reviewer_id: int, reason: str, now: datetime | None = None
) -> ProviderIngredientMapping:
    if not reason:
        raise ReviewError("rejection reason is required")
    now = now or datetime.now(UTC)
    row = _get(db, mapping_id)
    row.mapping_status = "rejected"
    row.required_review = False
    row.active = False
    row.reviewed_at = now
    row.reviewed_by = reviewer_id
    row.review_reason = reason
    row.evidence_json = {**(row.evidence_json or {}), "decision": "rejected", "reason": reason}
    db.flush()
    return row


def revoke(
    db: Session, mapping_id: int, *, reviewer_id: int, reason: str, now: datetime | None = None
) -> ProviderIngredientMapping:
    """Undo an approval WITHOUT deleting history — deactivate + record who/when/why. Idempotent."""
    if not reason:
        raise ReviewError("revoke reason is required")
    now = now or datetime.now(UTC)
    row = _get(db, mapping_id)
    row.active = False
    row.required_review = True
    row.mapping_status = "candidate"
    row.reviewed_at = now
    row.reviewed_by = reviewer_id
    row.review_reason = reason
    history = list((row.evidence_json or {}).get("revocations", []))
    history.append({"at": now.isoformat(), "by": reviewer_id, "reason": reason})
    row.evidence_json = {**(row.evidence_json or {}), "decision": "revoked", "revocations": history}
    db.flush()
    return row


def bulk_approve(
    db: Session,
    mapping_ids: list[int],
    *,
    reviewer_id: int,
    reason: str | None,
    min_confidence: Decimal = Decimal("0.75"),
    now: datetime | None = None,
) -> dict[str, object]:
    """Bulk-approve only when SAME ingredient, deterministic rule, no warnings, compatible
    category, resolved costing mode and confidence above threshold — never single-word ambiguous."""
    now = now or datetime.now(UTC)
    rows = [_get(db, mid) for mid in mapping_ids]
    if not rows:
        raise ReviewError("no mappings selected")
    if len({r.ingredient_id for r in rows}) != 1:
        raise ReviewError("bulk approval requires a single ingredient")
    for r in rows:
        warnings = (r.evidence_json or {}).get("warnings", [])
        if (
            r.mapping_status not in ("auto_approved", "candidate")
            or r.mapping_method not in ("exact_alias", "category_constrained")
            or (r.confidence_score or Decimal("0")) < min_confidence
            or r.unit_compatibility not in ("compatible", "convertible")
            or warnings
        ):
            raise ReviewError(
                f"mapping {r.id} is not eligible for bulk approval (ambiguous/low-confidence)"
            )
    approved = [approve(db, r.id, reviewer_id=reviewer_id, reason=reason, now=now).id for r in rows]
    return {"approved": approved}


def bulk_reject(
    db: Session,
    mapping_ids: list[int],
    *,
    reviewer_id: int,
    reason: str,
    now: datetime | None = None,
) -> dict[str, object]:
    rejected = [
        reject(db, mid, reviewer_id=reviewer_id, reason=reason, now=now).id for mid in mapping_ids
    ]
    return {"rejected": rejected}


def count_candidates(db: Session, **filters: object) -> int:
    stmt = _filtered(select(func.count(ProviderIngredientMapping.id)), **filters)
    return int(db.execute(stmt).scalar_one())


def _filtered(stmt: Select[Any], **f: object) -> Select[Any]:
    M = ProviderIngredientMapping
    conds = []
    if f.get("provider_code"):
        conds.append(M.provider_code == f["provider_code"])
    if f.get("retailer_slug"):
        conds.append(M.retailer_slug == f["retailer_slug"])
    if f.get("ingredient_id"):
        conds.append(M.ingredient_id == f["ingredient_id"])
    if f.get("canonical_ingredient_key"):
        conds.append(M.canonical_ingredient_key == f["canonical_ingredient_key"])
    if f.get("mapping_status"):
        conds.append(M.mapping_status == f["mapping_status"])
    if f.get("required_review") is not None:
        conds.append(M.required_review.is_(bool(f["required_review"])))
    if f.get("minimum_confidence") is not None:
        conds.append(M.confidence_score >= f["minimum_confidence"])
    if f.get("maximum_confidence") is not None:
        conds.append(M.confidence_score <= f["maximum_confidence"])
    conds.append(M.superseded_at.is_(None))  # never list superseded rows
    return stmt.where(*conds)


__all__ = [
    "MAPPING_VERSION",
    "ReviewError",
    "approve",
    "audit",
    "bulk_approve",
    "bulk_reject",
    "consolidate_duplicates",
    "count_candidates",
    "recipes_potentially_unlocked",
    "reject",
    "revoke",
]
