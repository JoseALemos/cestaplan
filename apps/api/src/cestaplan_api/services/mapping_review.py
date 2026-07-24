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

_HISTORIC_RELATIONS = {"superseded_exact_duplicate"}
_APPROVED = ("auto_approved", "manually_approved")


class ReviewError(Exception):
    """A review action was refused (bad state, ambiguous bulk, unknown id)."""


# --------------------------------------------------------------------------- #
# Explicit lifecycle semantics (§1) — NEVER collapse these into one `active` flag.
# --------------------------------------------------------------------------- #
def is_reviewable(row: ProviderIngredientMapping) -> bool:
    """Can an admin still see + act on this candidate? A competing/candidate/rejected row is
    reviewable; only an exact historic duplicate is hidden (still visible via a historic filter)."""
    return row.relation_status not in _HISTORIC_RELATIONS


def is_selectable_for_costing(row: ProviderIngredientMapping) -> bool:
    """Can this mapping be used to COST a recipe? Only an approved + active mapping — a competing
    candidate (active=false) is visible/approvable but NEVER used for costing until approved."""
    return bool(row.active and row.mapping_status in _APPROVED)


def lifecycle_status(row: ProviderIngredientMapping) -> str:
    """Coarse lifecycle independent of ``active``: approved | revoked | rejected |
    rejected_competitor | historic_duplicate | pending."""
    if row.mapping_status in _APPROVED:
        return "approved" if row.active else "revoked"
    if row.mapping_status == "rejected":
        return "rejected"
    if row.relation_status == "rejected_competitor":
        return "rejected_competitor"
    if row.relation_status == "superseded_exact_duplicate":
        return "historic_duplicate"
    return "pending"


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
    """Consolidate ONLY EXACT duplicates (same provider + ingredient + product + version).

    Competing candidates (same product, DIFFERENT ingredient) are NEVER consolidated here — they
    are conflicts to be resolved by review. Exact duplicates keep the best-confidence row and mark
    the rest ``superseded_exact_duplicate`` (kept for audit). Idempotent. In practice the unique
    index prevents exact duplicates, so this only tidies legacy rows.
    """
    now = now or datetime.now(UTC)
    rows = list(
        db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.superseded_at.is_(None)
            )
        ).scalars()
    )
    groups: dict[tuple[str, int, str, str], list[ProviderIngredientMapping]] = defaultdict(list)
    for r in rows:
        groups[(r.provider_code, r.ingredient_id, r.external_product_id, r.mapping_version)].append(
            r
        )
    superseded = 0
    for group in groups.values():
        if len(group) < 2:  # only EXACT duplicates (same 4-tuple) are consolidated
            continue
        winner = max(group, key=lambda m: (float(m.confidence_score or 0), m.active, -m.id))
        for m in group:
            if m.id == winner.id:
                continue
            m.superseded_at = now
            m.superseded_reason = f"exact duplicate of mapping {winner.id}"
            m.relation_status = "superseded_exact_duplicate"
            m.active = False
            superseded += 1
    db.flush()
    return {"exact_duplicate_groups": len(groups), "superseded_exact_duplicates": superseded}


def tag_conflicts(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """Assign a stable conflict_group_id to every product claimed by more than one ingredient
    and mark unresolved members ``competing``. Idempotent (re-runnable)."""
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
    conflicts = 0
    for (prov, ext), members in groups.items():
        if len({m.ingredient_id for m in members}) < 2:
            continue
        conflicts += 1
        gid = f"{prov}:{ext}"
        for m in members:
            m.conflict_group_id = gid
            if m.active and m.mapping_status in ("auto_approved", "manually_approved"):
                m.relation_status = "conflict_resolved"
            elif m.relation_status not in ("rejected_competitor",):
                m.relation_status = "competing"
    db.flush()
    return {"conflict_groups": conflicts}


# Explosion thresholds on multi_ingredient_product_ratio (fraction of products claimed by >1
# ingredient). Above CRITICAL a provider auto-approves NOTHING and requires review (§2).
EXPLOSION_WARNING = Decimal("0.30")
EXPLOSION_CRITICAL = Decimal("0.60")


def _ratio(n: int, d: int) -> Decimal:
    return Decimal("0") if d <= 0 else (Decimal(n) / Decimal(d)).quantize(Decimal("0.0001"))


def _explosion_block(metrics: dict[str, object]) -> dict[str, object]:
    """From the base metrics, derive the three documented ratios + threshold state."""
    unique = int(metrics["unique_products_discovered"])  # type: ignore[arg-type]
    pairs = int(metrics["candidate_pairs"])  # type: ignore[arg-type]
    multi = int(metrics["products_with_multiple_ingredient_candidates"])  # type: ignore[arg-type]
    groups = int(metrics["competing_candidate_groups"])  # type: ignore[arg-type]
    pairs_in_conflict = int(metrics["candidate_pairs_in_conflict_groups"])  # type: ignore[arg-type]
    multi_ratio = _ratio(multi, unique)
    state = (
        "critical" if multi_ratio > EXPLOSION_CRITICAL
        else "warning" if multi_ratio > EXPLOSION_WARNING
        else "ok"
    )
    return {
        # candidate_pair_ratio = candidate_pairs / unique_products_discovered
        "candidate_pair_ratio": str(_ratio(pairs, unique)),
        # multi_ingredient_product_ratio = multi-ingredient products / unique_products
        "multi_ingredient_product_ratio": str(multi_ratio),
        # average_candidates_per_conflict_group = pairs in conflict groups / conflict groups
        "average_candidates_per_conflict_group": str(_ratio(pairs_in_conflict, groups)),
        "explosion_state": state,
        "explosion_anomaly": state != "ok",
        "auto_approval_allowed": state != "critical",  # critical -> nothing auto-approves
    }


def candidate_metrics(db: Session, provider_code: str | None = None) -> dict[str, object]:
    """Candidate-explosion + conflict metrics (§2/§4). NEVER mixes providers when scoped to one.

    Returns global figures + the three documented ratios + a warning/critical state, plus
    per-ingredient breakdowns and the conflict-group-size distribution.
    """
    stmt = select(ProviderIngredientMapping).where(
        ProviderIngredientMapping.superseded_at.is_(None)
    )
    if provider_code:
        stmt = stmt.where(ProviderIngredientMapping.provider_code == provider_code)
    rows = list(db.execute(stmt).scalars())
    approved = [r for r in rows if r.active and r.mapping_status in _APPROVED]
    rejected = [r for r in rows if r.mapping_status in ("rejected", "rejected_competitor")]
    prod_to_ings: dict[tuple[str, str], set[int]] = defaultdict(set)
    prod_rows: dict[tuple[str, str], int] = Counter()
    for r in rows:
        prod_to_ings[(r.provider_code, r.external_product_id)].add(r.ingredient_id)
        prod_rows[(r.provider_code, r.external_product_id)] += 1
    multi_products = {k for k, s in prod_to_ings.items() if len(s) > 1}
    pairs_in_conflict = sum(prod_rows[k] for k in multi_products)
    unresolved = {
        gid
        for gid in {r.conflict_group_id for r in rows if r.conflict_group_id}
        if not any(
            r.conflict_group_id == gid and r.relation_status == "conflict_resolved" for r in rows
        )
    }
    # conflict-group-size distribution (how many candidates per conflict group).
    size_dist = Counter(prod_rows[k] for k in multi_products)
    per_ingredient: dict[str, int] = Counter(r.canonical_ingredient_key for r in rows)
    base: dict[str, object] = {
        "provider_code": provider_code or "all",
        "unique_products_discovered": len(prod_to_ings),
        "candidate_pairs": len(rows),
        "products_with_multiple_ingredient_candidates": len(multi_products),
        "competing_candidate_groups": len(multi_products),
        "candidate_pairs_in_conflict_groups": pairs_in_conflict,
        "approved_unique_products": len({r.external_product_id for r in approved}),
        "rejected_unique_products": len({r.external_product_id for r in rejected}),
        "unresolved_conflict_groups": len(unresolved),
        "candidates_per_ingredient": dict(per_ingredient),
        "conflict_group_size_distribution": {str(k): v for k, v in sorted(size_dist.items())},
    }
    base.update(_explosion_block(base))
    return base


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
    # Enforce one approved+active per product/provider (the partial unique index also guards this).
    other = (
        db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.provider_code == row.provider_code,
                ProviderIngredientMapping.external_product_id == row.external_product_id,
                ProviderIngredientMapping.id != row.id,
                ProviderIngredientMapping.active.is_(True),
                ProviderIngredientMapping.mapping_status.in_(
                    ("auto_approved", "manually_approved")
                ),
            )
        )
        .scalars()
        .first()
    )
    if other is not None:
        raise ReviewError(
            f"product already approved for ingredient {other.ingredient_id} (mapping {other.id})"
        )
    row.mapping_status = "manually_approved"
    row.required_review = False
    row.active = True
    row.reviewed_at = now
    row.reviewed_by = reviewer_id
    row.review_reason = reason
    row.relation_status = "conflict_resolved" if row.conflict_group_id else "independent"
    row.conflict_resolved_at = now if row.conflict_group_id else None
    row.evidence_json = {
        **(row.evidence_json or {}),
        "decision": "manually_approved",
        "reason": reason,
    }
    _resolve_competitors(db, row, now=now)
    db.flush()
    return row


def _resolve_competitors(db: Session, winner: ProviderIngredientMapping, *, now: datetime) -> None:
    """Losing competitors (same product, other ingredient) become rejected_competitor — kept for
    audit, never deleted, and never touching manually-rejected/incompatible siblings."""
    if winner.conflict_group_id is None:
        return
    siblings = db.execute(
        select(ProviderIngredientMapping).where(
            ProviderIngredientMapping.provider_code == winner.provider_code,
            ProviderIngredientMapping.external_product_id == winner.external_product_id,
            ProviderIngredientMapping.id != winner.id,
            ProviderIngredientMapping.superseded_at.is_(None),
        )
    ).scalars()
    for sib in siblings:
        if sib.mapping_status in ("rejected", "incompatible"):
            continue  # manual/deterministic decisions are preserved
        sib.relation_status = "rejected_competitor"
        sib.active = False
        sib.required_review = False
        sib.resolved_by_mapping_id = winner.id
        sib.conflict_resolved_at = now
        sib.conflict_reason = f"lost conflict to mapping {winner.id}"


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
    row.relation_status = "competing" if row.conflict_group_id else "independent"
    row.conflict_resolved_at = None
    row.reviewed_at = now
    row.reviewed_by = reviewer_id
    row.review_reason = reason
    history = list((row.evidence_json or {}).get("revocations", []))
    history.append({"at": now.isoformat(), "by": reviewer_id, "reason": reason})
    row.evidence_json = {**(row.evidence_json or {}), "decision": "revoked", "revocations": history}
    # Reopen eligible competitors that had been rejected only BECAUSE this one won.
    if row.conflict_group_id is not None:
        for sib in db.execute(
            select(ProviderIngredientMapping).where(
                ProviderIngredientMapping.provider_code == row.provider_code,
                ProviderIngredientMapping.external_product_id == row.external_product_id,
                ProviderIngredientMapping.id != row.id,
                ProviderIngredientMapping.relation_status == "rejected_competitor",
                ProviderIngredientMapping.resolved_by_mapping_id == row.id,
            )
        ).scalars():
            sib.relation_status = "competing"
            sib.required_review = True
            sib.mapping_status = "candidate"
            sib.resolved_by_mapping_id = None
            sib.conflict_reason = None
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
    if f.get("relation_status"):
        conds.append(M.relation_status == f["relation_status"])
    if f.get("required_review") is not None:
        conds.append(M.required_review.is_(bool(f["required_review"])))
    if f.get("minimum_confidence") is not None:
        conds.append(M.confidence_score >= f["minimum_confidence"])
    if f.get("maximum_confidence") is not None:
        conds.append(M.confidence_score <= f["maximum_confidence"])
    # Historic exact duplicates are hidden by default; a historic filter surfaces them.
    if not f.get("include_historic"):
        conds.append(M.relation_status != "superseded_exact_duplicate")
    return stmt.where(*conds)


__all__ = [
    "MAPPING_VERSION",
    "ReviewError",
    "approve",
    "audit",
    "bulk_approve",
    "bulk_reject",
    "candidate_metrics",
    "consolidate_duplicates",
    "count_candidates",
    "is_reviewable",
    "is_selectable_for_costing",
    "lifecycle_status",
    "recipes_potentially_unlocked",
    "reject",
    "revoke",
    "tag_conflicts",
]
