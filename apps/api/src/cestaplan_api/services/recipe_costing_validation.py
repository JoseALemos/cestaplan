"""Recipe costing readiness report (audit §6).

A read-only report that states whether ONE recipe is honestly fully costable and comparison
eligible for a provider, with the exact blockers/warnings and the input fingerprint. It never
changes production eligibility and never writes anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.models import ProductVariant, Recipe, Retailer
from cestaplan_api.services.mapping_enrichment import _DETAIL_CONTRACT_FINGERPRINT
from cestaplan_api.services.purchase_evidence import resolve_purchase_evidence
from cestaplan_api.services.recipe_costing import (
    PantryPolicy,
    _eligible_products,
    _variants_by_product,
    cost_recipe,
)
from cestaplan_api.services.recipe_shadow import comparison_input_fingerprint

# §6 blocker vocabulary.
_LEFTOVER_POLICY_ISOLATED = "not_amortized_isolated"


@dataclass(slots=True)
class RecipeCostingValidationReport:
    recipe_id: int
    recipe_version: str
    provider_code: str
    fully_costable: bool
    comparison_eligible: bool
    ingredient_count: int
    mandatory_ingredients: int
    optional_included: list[str]
    optional_excluded: list[str]
    resolved_products: int
    unresolved_products: int
    costing_modes: dict[str, int]
    scope_compatible: bool
    price_freshness_valid: bool
    input_fingerprint: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validated_at: str = ""

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


def _ingredient_blocker(
    db: Session,
    retailer_id: int,
    ingredient_id: int,
    required_unit: str,
    eligible: dict[int, list[int]],
    variants_by_product: dict[int, list[ProductVariant]],
    prices: CurrentPriceService,
    now: datetime,
    store_id: int | None,
) -> str:
    """Most informative §6 blocker for an uncostable mandatory ingredient (evidence-based)."""
    product_ids = eligible.get(ingredient_id, [])
    if not product_ids:
        return "incomplete_package_data"  # no active mapping/product to cost
    best_blocker = "incomplete_package_data"
    for pid in product_ids:
        for v in variants_by_product.get(pid, []):
            price = prices.current(db, v.id, store_id=store_id, as_of=now, staging=True)
            ev = resolve_purchase_evidence(
                name=v.display_name,
                required_unit=required_unit,
                net_content_quantity=v.net_content_quantity,
                net_content_unit=v.net_content_unit,
                variable_weight=bool(v.variable_weight),
                sell_unit=v.sell_unit,
                regular_price=price.amount if price else None,
                unit_price=v.unit_price,
                unit_price_unit=v.unit_price_unit,
                has_price=price is not None,
            )
            if ev.costing_eligible:
                return (
                    "stale_price"  # eligible product exists -> the block is elsewhere (freshness)
                )
            if ev.blocker:
                best_blocker = ev.blocker
    return best_blocker


def validate_recipe_costing(
    db: Session,
    recipe: Recipe,
    provider_code: str,
    *,
    pantry_policy: PantryPolicy = PantryPolicy.EMPTY_PANTRY,
    store_id: int | None = None,
    now: datetime | None = None,
) -> RecipeCostingValidationReport:
    """Validate a recipe's costing readiness for ``provider_code`` (read-only, §6)."""
    now = now or datetime.now(UTC)
    costing = cost_recipe(db, recipe, provider_code, pantry_policy=pantry_policy, now=now)
    mandatory = [line for line in costing.lines if not line.optional]
    resolved = [line for line in mandatory if line.costable]
    unresolved = [line for line in mandatory if not line.costable]

    modes: dict[str, int] = {}
    for line in resolved:
        if line.costing_mode:
            modes[line.costing_mode] = modes.get(line.costing_mode, 0) + 1

    scope_compatible = all(line.price_scope is not None for line in resolved)
    price_freshness_valid = all(line.fresh is not False for line in resolved)

    fp = comparison_input_fingerprint(
        recipe,
        servings=recipe.servings or 1,
        included_optionals=[],  # optionals excluded from the costed basket (§1)
        pantry_policy=pantry_policy.value,
        leftover_policy=_LEFTOVER_POLICY_ISOLATED,
    )

    blockers: list[str] = []
    warnings: list[str] = []

    # Per-ingredient blockers for anything uncostable.
    if unresolved:
        retailer_slug = costing.retailer_slug
        retailer_id = db.execute(
            select(Retailer.id).where(Retailer.slug == retailer_slug)
        ).scalar_one_or_none()
        eligible = _eligible_products(db, retailer_id, provider_code) if retailer_id else {}
        variants_by_product = _variants_by_product(db, retailer_id) if retailer_id else {}
        prices = CurrentPriceService()
        for line in unresolved:
            blk = (
                _ingredient_blocker(
                    db,
                    retailer_id,
                    line.ingredient_id,
                    line.required_unit,
                    eligible,
                    variants_by_product,
                    prices,
                    now,
                    store_id,
                )
                if retailer_id
                else "incomplete_package_data"
            )
            if blk not in blockers:
                blockers.append(blk)

    if not price_freshness_valid and "stale_price" not in blockers:
        blockers.append("stale_price")
    if not scope_compatible:
        blockers.append("incompatible_scope")
    # Enrichment contract: unknown detail contract blocks only enrichment (a warning here).
    if provider_code not in _DETAIL_CONTRACT_FINGERPRINT:
        warnings.append("enrichment_contract_unknown")

    fully_costable = costing.fully_costable and not any(
        b in blockers for b in ("stale_price", "incompatible_scope")
    )
    comparison_eligible = (
        fully_costable and scope_compatible and price_freshness_valid and not blockers
    )

    return RecipeCostingValidationReport(
        recipe_id=recipe.id,
        recipe_version=_recipe_version(recipe),
        provider_code=provider_code,
        fully_costable=fully_costable,
        comparison_eligible=comparison_eligible,
        ingredient_count=len(costing.lines),
        mandatory_ingredients=len(mandatory),
        optional_included=list(costing.optional_ingredients_included),
        optional_excluded=list(costing.optional_ingredients_excluded),
        resolved_products=len(resolved),
        unresolved_products=len(unresolved),
        costing_modes=modes,
        scope_compatible=scope_compatible,
        price_freshness_valid=price_freshness_valid,
        input_fingerprint=fp,
        blockers=blockers,
        warnings=warnings,
        validated_at=now.isoformat(),
    )


def _recipe_version(recipe: Recipe) -> str:
    updated = getattr(recipe, "updated_at", None)
    return updated.isoformat() if updated is not None else str(recipe.id)


__all__ = ["RecipeCostingValidationReport", "validate_recipe_costing"]
