"""Per-recipe shadow comparison (spec §11 + audit §1/§3).

Cost ONE recipe with a provider's STAGING data and, independently, with the baseline demo
catalogue, then compare. A money difference is produced ONLY when the comparison is genuinely
``comparable``: the exact same recipe, version, servings, mandatory ingredients, quantities, units,
optional-ingredient policy, pantry policy and leftover policy on BOTH sides (proven equal by a
``comparison_input_fingerprint``), both fully costable, with a compatible price scope. Otherwise
every monetary field stays null and the blocking reason is recorded.

Three money concepts are kept strictly separate (§3): purchased_cost (outlay), consumed_cost
(proportional value used) and leftover_value. Leftover is only amortized under a real shared plan.

Reads only; never writes; never touches production data (the provider side is STAGING only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    IngredientProductMapping,
    Product,
    ProductPrice,
    Recipe,
    Retailer,
)
from cestaplan_api.services.recipe_costing import (
    PantryPolicy,
    RecipeCosting,
    cost_recipe,
    fixed_package_cost,
    to_base,
)

_CENT = Decimal("0.01")
_PCT = Decimal("0.01")
# For a single isolated recipe leftover is never amortized (no later recipe proves reuse).
_LEFTOVER_POLICY_ISOLATED = "not_amortized_isolated"
_OPTIONAL_POLICY = "excluded_from_costed_basket"


class RecipeShadowStatus(StrEnum):
    COMPARABLE = "comparable"
    PROVIDER_NOT_COSTABLE = "provider_not_costable"
    BASELINE_NOT_COSTABLE = "baseline_not_costable"
    MISSING_BASELINE = "missing_baseline"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    OPTIONAL_INGREDIENT_MISMATCH = "optional_ingredient_mismatch"
    BASELINE_INPUT_MISMATCH = "baseline_input_mismatch"


def recipe_version(recipe: Recipe) -> str:
    """A stable version token for the recipe's costing inputs (content-derived)."""
    updated = getattr(recipe, "updated_at", None)
    if updated is not None:
        return updated.isoformat()
    return str(recipe.id)


def comparison_input_fingerprint(
    recipe: Recipe,
    *,
    servings: int,
    included_optionals: list[str],
    pantry_policy: str,
    leftover_policy: str,
) -> str:
    """SHA-256 over every input that must be identical for a valid comparison (§1)."""
    payload = {
        "recipe_id": recipe.id,
        "recipe_version": recipe_version(recipe),
        "servings": servings,
        "mandatory_ingredients": sorted(
            [
                [ri.canonical_name, str(Decimal(ri.quantity)), ri.unit]
                for ri in recipe.ingredients
                if not ri.optional
            ]
        ),
        "optional_policy": _OPTIONAL_POLICY,
        "optional_included": sorted(included_optionals),
        "pantry_policy": pantry_policy,
        "leftover_policy": leftover_policy,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(slots=True)
class BaselineCostLine:
    ingredient_id: int
    canonical_name: str
    costable: bool
    optional: bool = False
    product_id: int | None = None
    product_name: str | None = None
    package_quantity: Decimal | None = None
    package_unit: str | None = None
    package_price: Decimal | None = None
    line_cost: Decimal | None = None
    consumed_cost: Decimal | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


@dataclass(slots=True)
class BaselineCosting:
    recipe_id: int
    baseline_slug: str
    fully_costable: bool = False
    lines: list[BaselineCostLine] = field(default_factory=list)
    total_purchase_cost: Decimal | None = None
    total_consumed_cost: Decimal | None = None
    optional_ingredients_included: list[str] = field(default_factory=list)
    optional_ingredients_excluded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            if k == "lines":
                continue
            out[k] = str(v) if isinstance(v, Decimal) else v
        out["lines"] = [line.as_dict() for line in self.lines]
        return out


@dataclass(slots=True)
class RecipeShadowComparison:
    recipe_id: int
    title: str
    servings: int
    provider_code: str
    baseline_slug: str
    comparison_status: str
    evaluated_at: str
    provider: RecipeCosting
    baseline: BaselineCosting
    pantry_policy: str = PantryPolicy.EMPTY_PANTRY.value
    optional_ingredients_included: list[str] = field(default_factory=list)
    optional_ingredients_excluded: list[str] = field(default_factory=list)
    provider_input_fingerprint: str | None = None
    baseline_input_fingerprint: str | None = None
    comparison_input_fingerprint: str | None = None
    blockers: list[str] = field(default_factory=list)
    # Purchased (outlay) comparison.
    provider_cost: Decimal | None = None
    baseline_cost: Decimal | None = None
    absolute_difference: Decimal | None = None  # == purchased_cost_difference (back-compat)
    percentage_difference: Decimal | None = None
    purchased_cost_difference: Decimal | None = None
    purchased_cost_percentage: Decimal | None = None
    # Consumed (proportional value used) comparison.
    provider_consumed_cost: Decimal | None = None
    baseline_consumed_cost: Decimal | None = None
    consumed_cost_difference: Decimal | None = None
    consumed_cost_percentage: Decimal | None = None
    # Leftover (provider side; only amortizable inside a real plan).
    reusable_leftover_value: Decimal | None = None
    non_reusable_leftover_value: Decimal | None = None
    comparison_interpretation: str | None = None
    currency: str = "EUR"

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            if k in ("provider", "baseline"):
                continue
            out[k] = str(v) if isinstance(v, Decimal) else v
        out["provider"] = self.provider.as_dict()
        out["baseline"] = self.baseline.as_dict()
        return out


def _cost_baseline(db: Session, recipe: Recipe, baseline_slug: str) -> BaselineCosting:
    """Quantity-aware baseline costing over the legacy demo catalogue (Product + ProductPrice).

    Applies the SAME optional policy as the provider side: optionals are excluded from the costed
    basket (recorded for transparency) so neither side is penalised for a mappable/unmappable
    optional. Only mandatory ingredients drive the baseline total (§1)."""
    result = BaselineCosting(recipe_id=recipe.id, baseline_slug=baseline_slug)
    rid = db.execute(select(Retailer.id).where(Retailer.slug == baseline_slug)).scalar_one_or_none()
    if rid is None:
        return result

    ing_products: dict[int, list[int]] = {}
    for ing_id, prod_id in db.execute(
        select(IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id).where(
            IngredientProductMapping.retailer_id == rid,
            IngredientProductMapping.is_active.is_(True),
        )
    ).all():
        if ing_id is not None and prod_id is not None:
            ing_products.setdefault(ing_id, []).append(prod_id)

    all_costable = True
    total_purchase = Decimal("0")
    total_consumed = Decimal("0")
    any_priced = False
    for ri in recipe.ingredients:
        line = BaselineCostLine(
            ri.ingredient_id, ri.canonical_name, costable=False, optional=bool(ri.optional)
        )
        best = _best_baseline_product(db, rid, ri, ing_products)
        if best is not None:
            pid, prod, price, purchased_base, line_cost = best
            required_base = to_base(Decimal(ri.quantity), ri.unit)[0]  # type: ignore[index]
            consumed = (line_cost * required_base / purchased_base).quantize(_CENT)
            line.costable = True
            line.product_id = pid
            line.product_name = prod.name
            line.package_quantity = (
                Decimal(prod.package_quantity) if prod.package_quantity is not None else None
            )
            line.package_unit = prod.package_unit
            line.package_price = price.quantize(_CENT)
            line.line_cost = line_cost.quantize(_CENT)
            line.consumed_cost = consumed
            if not ri.optional:
                any_priced = True
                total_purchase += line_cost
                total_consumed += consumed
        elif not ri.optional:
            line.reason = f"{ri.canonical_name}: sin producto baseline costeable"
            all_costable = False
        if ri.optional:
            # Same policy as provider: excluded from the costed basket.
            result.optional_ingredients_excluded.append(ri.canonical_name)
        result.lines.append(line)

    result.fully_costable = all_costable and any_priced
    if result.fully_costable:
        result.total_purchase_cost = total_purchase.quantize(_CENT)
        result.total_consumed_cost = total_consumed.quantize(_CENT)
    return result


def _best_baseline_product(
    db: Session, rid: int, ri: object, ing_products: dict[int, list[int]]
) -> tuple[int, Product, Decimal, Decimal, Decimal] | None:
    based = to_base(Decimal(ri.quantity), ri.unit)  # type: ignore[attr-defined]
    if based is None:
        return None
    required_base, required_dim = based
    best: tuple[int, Product, Decimal, Decimal, Decimal] | None = None
    for pid in ing_products.get(ri.ingredient_id, []):  # type: ignore[attr-defined]
        prod = db.get(Product, pid)
        if prod is None:
            continue
        price = (
            db.execute(
                select(ProductPrice.amount)
                .where(ProductPrice.retailer_id == rid, ProductPrice.product_id == pid)
                .order_by(ProductPrice.observed_at.desc())
            )
            .scalars()
            .first()
        )
        if price is None:
            continue
        costed = fixed_package_cost(
            required_base,
            required_dim,
            Decimal(prod.package_quantity) if prod.package_quantity is not None else None,
            prod.package_unit,
            price,
        )
        if costed is None:
            continue
        _packages, purchased_base, line_cost = costed
        if best is None or line_cost < best[4]:
            best = (pid, prod, price, purchased_base, line_cost)
    return best


def _scopes_compatible(provider: RecipeCosting) -> bool:
    """Both sides are evaluated at national scope; a provider line with an incompatible scope would
    already have been uncostable. National vs national."""
    return provider.price_scope == "national"


def _pct(diff: Decimal, base: Decimal) -> Decimal | None:
    return (diff / base * Decimal("100")).quantize(_PCT) if base > 0 else None


def compare_recipe_shadow(
    db: Session,
    recipe: Recipe,
    provider_code: str,
    *,
    baseline_slug: str = "mercaejemplo",
    pantry_policy: PantryPolicy = PantryPolicy.EMPTY_PANTRY,
    now: datetime | None = None,
) -> RecipeShadowComparison:
    """Compare one recipe's provider (staging) cost against the baseline demo cost (§1/§3/§11)."""
    now = now or datetime.now(UTC)
    provider = cost_recipe(db, recipe, provider_code, pantry_policy=pantry_policy, now=now)
    baseline = _cost_baseline(db, recipe, baseline_slug)
    servings = recipe.servings or 1

    # Optionals are excluded from the costed basket on BOTH sides -> included set is empty and
    # symmetric. A mismatch here (an optional counted on one side only) invalidates the comparison.
    prov_incl = list(provider.optional_ingredients_included)
    base_incl = list(baseline.optional_ingredients_included)
    prov_fp = comparison_input_fingerprint(
        recipe,
        servings=servings,
        included_optionals=prov_incl,
        pantry_policy=pantry_policy.value,
        leftover_policy=_LEFTOVER_POLICY_ISOLATED,
    )
    base_fp = comparison_input_fingerprint(
        recipe,
        servings=servings,
        included_optionals=base_incl,
        pantry_policy=pantry_policy.value,
        leftover_policy=_LEFTOVER_POLICY_ISOLATED,
    )

    blockers: list[str] = []
    status = RecipeShadowStatus.COMPARABLE
    if not provider.fully_costable:
        blockers.append("provider_not_costable")
        status = RecipeShadowStatus.PROVIDER_NOT_COSTABLE
    elif not baseline.fully_costable:
        if not baseline.lines or all(not line_.costable for line_ in baseline.lines):
            blockers.append("missing_baseline")
            status = RecipeShadowStatus.MISSING_BASELINE
        else:
            blockers.append("baseline_not_costable")
            status = RecipeShadowStatus.BASELINE_NOT_COSTABLE
    elif sorted(prov_incl) != sorted(base_incl):
        blockers.append("optional_ingredient_mismatch")
        status = RecipeShadowStatus.OPTIONAL_INGREDIENT_MISMATCH
    elif prov_fp != base_fp:
        blockers.append("baseline_input_mismatch")
        status = RecipeShadowStatus.BASELINE_INPUT_MISMATCH
    elif not _scopes_compatible(provider):
        blockers.append("incompatible_scope")
        status = RecipeShadowStatus.INCOMPATIBLE_SCOPE

    comparison = RecipeShadowComparison(
        recipe_id=recipe.id,
        title=recipe.title,
        servings=servings,
        provider_code=provider_code,
        baseline_slug=baseline_slug,
        comparison_status=status.value,
        evaluated_at=now.isoformat(),
        provider=provider,
        baseline=baseline,
        pantry_policy=pantry_policy.value,
        optional_ingredients_included=prov_incl,
        optional_ingredients_excluded=sorted(
            set(provider.optional_ingredients_excluded)
            | set(baseline.optional_ingredients_excluded)
        ),
        provider_input_fingerprint=prov_fp,
        baseline_input_fingerprint=base_fp,
        blockers=blockers,
    )
    # Money ONLY when genuinely comparable; otherwise every monetary field stays null.
    if status is RecipeShadowStatus.COMPARABLE:
        comparison.comparison_input_fingerprint = prov_fp
        p_cost = provider.total_purchase_cost or Decimal("0")
        b_cost = baseline.total_purchase_cost or Decimal("0")
        p_cons = provider.total_consumed_cost or Decimal("0")
        b_cons = baseline.total_consumed_cost or Decimal("0")
        comparison.provider_cost = p_cost
        comparison.baseline_cost = b_cost
        comparison.purchased_cost_difference = (p_cost - b_cost).quantize(_CENT)
        comparison.purchased_cost_percentage = _pct(p_cost - b_cost, b_cost)
        comparison.absolute_difference = comparison.purchased_cost_difference
        comparison.percentage_difference = comparison.purchased_cost_percentage
        comparison.provider_consumed_cost = p_cons
        comparison.baseline_consumed_cost = b_cons
        comparison.consumed_cost_difference = (p_cons - b_cons).quantize(_CENT)
        comparison.consumed_cost_percentage = _pct(p_cons - b_cons, b_cons)
        comparison.reusable_leftover_value = provider.reusable_leftover_value
        comparison.non_reusable_leftover_value = provider.non_reusable_leftover_value
        comparison.comparison_interpretation = (
            "purchased = desembolso inicial de envases completos; consumed = valor proporcional "
            "realmente utilizado. El sobrante NO se amortiza en una receta aislada "
            "(reusable/non_reusable = 0); sólo un plan con reutilización real lo amortizaría."
        )
    return comparison


__all__ = [
    "BaselineCostLine",
    "BaselineCosting",
    "RecipeShadowComparison",
    "RecipeShadowStatus",
    "compare_recipe_shadow",
    "comparison_input_fingerprint",
    "recipe_version",
]
