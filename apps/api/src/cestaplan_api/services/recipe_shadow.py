"""Per-recipe shadow comparison (spec §11).

Cost ONE recipe with a provider's STAGING data and, independently, with the baseline demo catalogue,
then compare. A money difference is produced ONLY when the comparison is genuinely ``comparable``:
the exact same recipe, quantities and servings must be fully costable on BOTH sides with a
compatible price scope. Otherwise the comparison is left without monetary figures (never a zero,
fabricated saving) and the blocking reason is recorded.

Reads only; never writes; never touches production data (the provider side is STAGING only).
"""

from __future__ import annotations

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
    RecipeCosting,
    cost_recipe,
    fixed_package_cost,
    to_base,
)

_CENT = Decimal("0.01")


class RecipeShadowStatus(StrEnum):
    COMPARABLE = "comparable"
    PROVIDER_NOT_COSTABLE = "provider_not_costable"
    BASELINE_NOT_COSTABLE = "baseline_not_costable"
    MISSING_BASELINE = "missing_baseline"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


@dataclass(slots=True)
class BaselineCostLine:
    ingredient_id: int
    canonical_name: str
    costable: bool
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
    blockers: list[str] = field(default_factory=list)
    absolute_difference: Decimal | None = None
    percentage_difference: Decimal | None = None
    provider_cost: Decimal | None = None
    baseline_cost: Decimal | None = None
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
    """Quantity-aware baseline costing over the legacy demo catalogue (Product + ProductPrice)."""
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
        line = BaselineCostLine(ri.ingredient_id, ri.canonical_name, costable=False)
        based = to_base(Decimal(ri.quantity), ri.unit)
        best: tuple[int, Product, Decimal, Decimal, Decimal] | None = None
        if based is not None:
            required_base, required_dim = based
            for pid in ing_products.get(ri.ingredient_id, []):
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
        else:
            line.reason = f"{ri.canonical_name}: sin producto baseline costeable"
            if not ri.optional:
                all_costable = False
        result.lines.append(line)

    result.fully_costable = all_costable and any_priced
    if result.fully_costable:
        result.total_purchase_cost = total_purchase.quantize(_CENT)
        result.total_consumed_cost = total_consumed.quantize(_CENT)
    return result


def _scopes_compatible(provider: RecipeCosting, baseline_slug: str) -> bool:
    """Both sides are evaluated at national scope here; a provider line with an incompatible scope
    (already filtered by the costing engine) would have made it uncostable. National vs national."""
    return provider.price_scope == "national"


def compare_recipe_shadow(
    db: Session,
    recipe: Recipe,
    provider_code: str,
    *,
    baseline_slug: str = "mercaejemplo",
    now: datetime | None = None,
) -> RecipeShadowComparison:
    """Compare one recipe's provider (staging) cost against the baseline demo cost (§11)."""
    now = now or datetime.now(UTC)
    provider = cost_recipe(db, recipe, provider_code, now=now)
    baseline = _cost_baseline(db, recipe, baseline_slug)

    blockers: list[str] = []
    status = RecipeShadowStatus.COMPARABLE
    if not provider.fully_costable:
        blockers.append("provider_not_costable")
        status = RecipeShadowStatus.PROVIDER_NOT_COSTABLE
    elif baseline.total_purchase_cost is None and not baseline.fully_costable:
        # Distinguish "no baseline at all" from "baseline present but not fully costable".
        if not baseline.lines or all(not line_.costable for line_ in baseline.lines):
            blockers.append("missing_baseline")
            status = RecipeShadowStatus.MISSING_BASELINE
        else:
            blockers.append("baseline_not_costable")
            status = RecipeShadowStatus.BASELINE_NOT_COSTABLE
    elif not baseline.fully_costable:
        blockers.append("baseline_not_costable")
        status = RecipeShadowStatus.BASELINE_NOT_COSTABLE
    elif not _scopes_compatible(provider, baseline_slug):
        blockers.append("incompatible_scope")
        status = RecipeShadowStatus.INCOMPATIBLE_SCOPE

    comparison = RecipeShadowComparison(
        recipe_id=recipe.id,
        title=recipe.title,
        servings=recipe.servings or 1,
        provider_code=provider_code,
        baseline_slug=baseline_slug,
        comparison_status=status.value,
        evaluated_at=now.isoformat(),
        provider=provider,
        baseline=baseline,
        blockers=blockers,
    )
    # Money ONLY when genuinely comparable; otherwise leave every monetary field null.
    if status is RecipeShadowStatus.COMPARABLE:
        p_cost = provider.total_purchase_cost or Decimal("0")
        b_cost = baseline.total_purchase_cost or Decimal("0")
        comparison.provider_cost = p_cost
        comparison.baseline_cost = b_cost
        comparison.absolute_difference = (p_cost - b_cost).quantize(_CENT)
        if b_cost > 0:
            comparison.percentage_difference = (
                (p_cost - b_cost) / b_cost * Decimal("100")
            ).quantize(Decimal("0.01"))
    return comparison


__all__ = [
    "BaselineCostLine",
    "BaselineCosting",
    "RecipeShadowComparison",
    "RecipeShadowStatus",
    "compare_recipe_shadow",
]
