"""Quantity-aware recipe costing engine (spec §9).

Given a recipe and a provider, compute what it *actually* costs to buy the ingredients, honouring
real purchasing rules:

* ``fixed_package``  -> you must buy whole packages: ``packages = ceil(required / pack_qty)``.
* ``discrete_unit``  -> you must buy whole buyable units.
* ``variable_weight`` / ``variable_volume`` -> buy the required amount rounded up to the sellable
  increment, priced at the genuine per-weight/volume sell price.

For every ingredient the engine records the purchased quantity, the consumed quantity, the surplus
(and its value), the costing mode and the exact package maths, so the result is fully auditable.

Hard rules (never violated):
* never buy a fractional fixed package or discrete unit;
* never use a zero/negative price, or an informational ``unit_price`` as a fixed-package price;
* never mix incompatible units (mass vs volume vs count) between recipe and product;
* only STAGING prices are read — never production data;
* only ``active`` mappings/variants are eligible — pending candidates are ignored.

The engine reads only; it writes nothing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.current_price import CurrentPriceService, FreshnessStatus
from cestaplan_api.ingestion.providers.contracts import ProductCostingMode
from cestaplan_api.ingestion.providers.onboarding import classify_variant_costing_mode, get_entry
from cestaplan_api.models import (
    IngredientProductMapping,
    ProductVariant,
    ProviderIngredientMapping,
    Recipe,
    RecipeIngredient,
    Retailer,
)

_CENT = Decimal("0.01")
_QTY = Decimal("0.0001")


class PantryPolicy(StrEnum):
    """How stock/leftovers are treated when costing (spec §4)."""

    EMPTY_PANTRY = "empty_pantry"  # buy every package needed; purchased_cost = full packages
    USE_EXISTING_STOCK = "use_existing_stock"  # subtract real pantry stock (requires inventory)
    PLAN_SHARED_INVENTORY = "plan_shared_inventory"  # leftovers may carry to later plan recipes


# Canonical base unit per physical dimension (so a recipe's grams and a pack's kilograms compare).
_DIMENSION: dict[str, str] = {
    "g": "mass",
    "kg": "mass",
    "ml": "volume",
    "l": "volume",
    "unit": "count",
}
_TO_BASE: dict[str, Decimal] = {
    "g": Decimal("1"),
    "kg": Decimal("1000"),
    "ml": Decimal("1"),
    "l": Decimal("1000"),
    "unit": Decimal("1"),
}


def _to_base(quantity: Decimal, unit: str) -> tuple[Decimal, str] | None:
    """Convert ``(quantity, unit)`` to its canonical base amount + dimension, or None if unknown."""
    dim = _DIMENSION.get(unit)
    if dim is None:
        return None
    return quantity * _TO_BASE[unit], dim


@dataclass(slots=True)
class IngredientCostLine:
    ingredient_id: int
    canonical_name: str
    display_name: str
    required_quantity: Decimal
    required_unit: str
    optional: bool
    costable: bool = False
    reason: str | None = None
    # selected product
    provider_code: str | None = None
    product_id: int | None = None
    variant_id: int | None = None
    external_product_id: str | None = None
    product_name: str | None = None
    costing_mode: str | None = None
    price_scope: str | None = None
    fresh: bool | None = None
    package_quantity: Decimal | None = None
    package_unit: str | None = None
    package_price: Decimal | None = None
    # purchasing maths
    units_purchased: Decimal | None = None
    purchased_quantity: Decimal | None = None
    consumed_quantity: Decimal | None = None
    surplus_quantity: Decimal | None = None
    line_cost: Decimal | None = None
    consumed_cost: Decimal | None = None
    surplus_value: Decimal | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


@dataclass(slots=True)
class RecipeCosting:
    recipe_id: int
    title: str
    servings: int
    provider_code: str
    retailer_slug: str
    price_scope: str
    evaluated_at: str
    fully_costable: bool = False
    pantry_policy: str = PantryPolicy.EMPTY_PANTRY.value
    lines: list[IngredientCostLine] = field(default_factory=list)
    # Three separate money concepts (§3): outlay, value actually used, and leftover value.
    total_purchase_cost: Decimal | None = None  # purchased_cost: full-package outlay
    total_consumed_cost: Decimal | None = None  # consumed_cost: proportional value used
    total_leftover_value: Decimal | None = None  # leftover_value: surplus value
    reusable_leftover_value: Decimal | None = None  # only amortizable under a real plan
    non_reusable_leftover_value: Decimal | None = None  # inherently discarded leftover
    total_surplus_value: Decimal | None = None  # alias of total_leftover_value (back-compat)
    cost_per_serving_purchase: Decimal | None = None
    cost_per_serving_consumed: Decimal | None = None
    currency: str = "EUR"
    optional_ingredients_included: list[str] = field(default_factory=list)
    optional_ingredients_excluded: list[str] = field(default_factory=list)
    uncostable_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            if k == "lines":
                continue
            out[k] = str(v) if isinstance(v, Decimal) else v
        out["lines"] = [line.as_dict() for line in self.lines]
        return out


@dataclass(slots=True)
class _Candidate:
    variant: ProductVariant
    price: Decimal
    price_scope: str
    fresh: bool
    mode: ProductCostingMode


def _eligible_products(db: Session, retailer_id: int, provider_code: str) -> dict[int, list[int]]:
    """Ingredient -> product_ids from ACTIVE legacy + provider mappings (pending never eligible)."""
    out: dict[int, list[int]] = {}
    for ing_id, prod_id in db.execute(
        select(IngredientProductMapping.ingredient_id, IngredientProductMapping.product_id).where(
            IngredientProductMapping.retailer_id == retailer_id,
            IngredientProductMapping.is_active.is_(True),
        )
    ).all():
        if ing_id is not None and prod_id is not None:
            out.setdefault(ing_id, []).append(prod_id)
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
            out.setdefault(ing_id, []).append(prod_id)
    return out


def _variants_by_product(db: Session, retailer_id: int) -> dict[int, list[ProductVariant]]:
    out: dict[int, list[ProductVariant]] = {}
    for v in db.execute(
        select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id, ProductVariant.active.is_(True)
        )
    ).scalars():
        if v.product_id is not None:
            out.setdefault(v.product_id, []).append(v)
    return out


def fixed_package_cost(
    required_base: Decimal,
    required_dim: str,
    pack_quantity: Decimal | None,
    pack_unit: str | None,
    price: Decimal,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Whole-package maths for a fixed package: ``ceil(required/pack)`` packages, never fractional.

    Returns ``(packages, purchased_base, line_cost)`` or None when the pack is unusable or its unit
    is incompatible with the required dimension. Shared by provider and baseline costing so both
    sides apply identical rules. A zero/negative price is never buyable.
    """
    if price <= 0 or pack_quantity is None or pack_unit is None:
        return None
    pack = _to_base(pack_quantity, pack_unit)
    if pack is None or pack[1] != required_dim or pack[0] <= 0:
        return None
    pack_base = pack[0]
    packages = (required_base / pack_base).to_integral_value(rounding=ROUND_CEILING)
    if packages < 1:
        packages = Decimal("1")
    return packages, packages * pack_base, packages * price


def _cost_candidate(
    required_base: Decimal, required_dim: str, cand: _Candidate
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return (units_purchased, purchased_base, line_cost) for a candidate, or None if not buyable.

    Enforces the per-mode purchasing rules; unit/dimension incompatibility -> None.
    """
    v = cand.variant
    if cand.price <= 0:
        return None
    mode = cand.mode
    if mode in (ProductCostingMode.FIXED_PACKAGE, ProductCostingMode.DISCRETE_UNIT):
        if v.net_content_quantity is None or v.net_content_unit is None:
            # A discrete unit with no net content is one buyable piece per unit.
            if mode is ProductCostingMode.DISCRETE_UNIT and required_dim == "count":
                units = required_base.to_integral_value(rounding=ROUND_CEILING)
                return units, units, (units * cand.price)
            return None
        return fixed_package_cost(
            required_base, required_dim, v.net_content_quantity, v.net_content_unit, cand.price
        )
    if mode in (ProductCostingMode.VARIABLE_WEIGHT, ProductCostingMode.VARIABLE_VOLUME):
        # Genuine per-weight/volume sell price (not the informational unit_price of a package).
        if v.unit_price is None or v.unit_price_unit is None:
            return None
        sell = _to_base(Decimal("1"), v.unit_price_unit)
        if sell is None or sell[1] != required_dim:
            return None
        # Sellable increment: smallest base step (1 g / 1 ml). Buy required rounded up to it.
        purchased_base = required_base.to_integral_value(rounding=ROUND_CEILING)
        if purchased_base < 1:
            purchased_base = Decimal("1")
        price_per_base = cand.price / sell[0]  # unit_price is per unit_price_unit
        return purchased_base, purchased_base, (purchased_base * price_per_base)
    return None


def _best_candidate(
    db: Session,
    prices: CurrentPriceService,
    product_ids: list[int],
    variants_by_product: dict[int, list[ProductVariant]],
    required_base: Decimal,
    required_dim: str,
    *,
    store_id: int | None,
    now: datetime,
) -> tuple[_Candidate, Decimal, Decimal, Decimal] | None:
    """Cheapest buyable candidate for the required amount: min total line cost, then price, id."""
    best: tuple[_Candidate, Decimal, Decimal, Decimal] | None = None
    for product_id in product_ids:
        for v in variants_by_product.get(product_id, []):
            price = prices.current(db, v.id, store_id=store_id, as_of=now, staging=True)
            if price is None:
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
            if mode is ProductCostingMode.UNRESOLVED:
                continue
            cand = _Candidate(
                variant=v,
                price=price.amount,
                price_scope=price.price_scope,
                fresh=price.status is FreshnessStatus.FRESH,
                mode=mode,
            )
            costed = _cost_candidate(required_base, required_dim, cand)
            if costed is None:
                continue
            units, purchased_base, line_cost = costed
            key = (line_cost, cand.price, v.id)
            if best is None or key < (best[3], best[0].price, best[0].variant.id):
                best = (cand, units, purchased_base, line_cost)
    return best


def cost_recipe(
    db: Session,
    recipe: Recipe,
    provider_code: str,
    *,
    store_id: int | None = None,
    pantry_policy: PantryPolicy = PantryPolicy.EMPTY_PANTRY,
    now: datetime | None = None,
) -> RecipeCosting:
    """Cost every mandatory ingredient of ``recipe`` with ``provider_code`` STAGING data (§9).

    ``pantry_policy`` (§4) declares how leftovers/stock are treated. For a single isolated recipe
    only ``empty_pantry`` is meaningful (buy full packages); leftover is NOT amortized here — a
    reusable/non-reusable split only becomes non-zero under a real ``plan_shared_inventory``.
    """
    now = now or datetime.now(UTC)
    entry = get_entry(provider_code)
    retailer_slug = entry.retailer_slug if entry else provider_code
    required_scope = "exact_store" if store_id else "national"
    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == retailer_slug)
    ).scalar_one_or_none()

    result = RecipeCosting(
        recipe_id=recipe.id,
        title=recipe.title,
        servings=recipe.servings or 1,
        provider_code=provider_code,
        retailer_slug=retailer_slug,
        price_scope=required_scope,
        evaluated_at=now.isoformat(),
        pantry_policy=pantry_policy.value,
    )
    if retailer_id is None:
        result.uncostable_reasons.append("retailer no encontrado")
        return result

    eligible = _eligible_products(db, retailer_id, provider_code)
    variants_by_product = _variants_by_product(db, retailer_id)
    prices = CurrentPriceService()

    mandatory_costable = True
    total_purchase = Decimal("0")
    total_consumed = Decimal("0")
    total_surplus = Decimal("0")
    any_priced = False

    for ri in recipe.ingredients:
        line = _cost_line(db, ri, eligible, variants_by_product, prices, store_id=store_id, now=now)
        result.lines.append(line)
        if ri.optional:
            # Default optional policy (§1/§4): optionals are EXCLUDED from the costed basket so a
            # provider that cannot map an optional is never penalised vs one that can. The line is
            # still costed for transparency (line.costable), but its money is not summed.
            result.optional_ingredients_excluded.append(ri.canonical_name)
            continue
        if line.costable and line.line_cost is not None:
            any_priced = True
            total_purchase += line.line_cost
            total_consumed += line.consumed_cost or Decimal("0")
            total_surplus += line.surplus_value or Decimal("0")
        else:
            mandatory_costable = False
            result.uncostable_reasons.append(line.reason or f"{ri.canonical_name}: no calculable")

    result.fully_costable = mandatory_costable and any_priced
    if result.fully_costable:
        result.total_purchase_cost = total_purchase.quantize(_CENT)
        result.total_consumed_cost = total_consumed.quantize(_CENT)
        leftover = total_surplus.quantize(_CENT)
        result.total_leftover_value = leftover
        result.total_surplus_value = leftover  # back-compat alias
        # For an isolated recipe leftover is NOT amortized: neither assumed reused nor wasted.
        # A non-zero reusable/non-reusable split only arises inside a real shared plan.
        result.reusable_leftover_value = Decimal("0.00")
        result.non_reusable_leftover_value = Decimal("0.00")
        servings = Decimal(result.servings or 1)
        result.cost_per_serving_purchase = (total_purchase / servings).quantize(_CENT)
        result.cost_per_serving_consumed = (total_consumed / servings).quantize(_CENT)
    return result


def _cost_line(
    db: Session,
    ri: RecipeIngredient,
    eligible: dict[int, list[int]],
    variants_by_product: dict[int, list[ProductVariant]],
    prices: CurrentPriceService,
    *,
    store_id: int | None,
    now: datetime,
) -> IngredientCostLine:
    line = IngredientCostLine(
        ingredient_id=ri.ingredient_id,
        canonical_name=ri.canonical_name,
        display_name=ri.display_name or ri.canonical_name,
        required_quantity=Decimal(ri.quantity),
        required_unit=ri.unit,
        optional=bool(ri.optional),
    )
    based = _to_base(Decimal(ri.quantity), ri.unit)
    if based is None:
        line.reason = f"{ri.canonical_name}: unidad de receta desconocida ({ri.unit})"
        return line
    required_base, required_dim = based
    product_ids = eligible.get(ri.ingredient_id, [])
    if not product_ids:
        line.reason = f"{ri.canonical_name}: sin mapeo activo"
        return line
    best = _best_candidate(
        db,
        prices,
        product_ids,
        variants_by_product,
        required_base,
        required_dim,
        store_id=store_id,
        now=now,
    )
    if best is None:
        line.reason = f"{ri.canonical_name}: sin producto costeable (precio/unidad/envase)"
        return line
    cand, units, purchased_base, line_cost = best
    v = cand.variant
    consumed_ratio = required_base / purchased_base if purchased_base > 0 else Decimal("0")
    consumed_cost = (line_cost * consumed_ratio).quantize(_CENT)
    line.costable = True
    line.product_id = v.product_id
    line.variant_id = v.id
    line.external_product_id = str(v.external_product_id) if v.external_product_id else None
    line.product_name = v.display_name
    line.costing_mode = cand.mode.value
    line.price_scope = cand.price_scope
    line.fresh = cand.fresh
    line.package_quantity = (
        Decimal(v.net_content_quantity) if v.net_content_quantity is not None else None
    )
    line.package_unit = v.net_content_unit
    line.package_price = cand.price.quantize(_CENT)
    line.units_purchased = units.quantize(Decimal("1"))
    line.purchased_quantity = purchased_base.quantize(_QTY)
    line.consumed_quantity = required_base.quantize(_QTY)
    line.surplus_quantity = (purchased_base - required_base).quantize(_QTY)
    line.line_cost = line_cost.quantize(_CENT)
    line.consumed_cost = consumed_cost
    line.surplus_value = (line.line_cost - consumed_cost).quantize(_CENT)
    return line


__all__ = [
    "IngredientCostLine",
    "PantryPolicy",
    "RecipeCosting",
    "cost_recipe",
    "fixed_package_cost",
    "to_base",
]


def to_base(quantity: Decimal, unit: str) -> tuple[Decimal, str] | None:
    """Public wrapper of the canonical unit conversion (quantity+unit -> base + dimension)."""
    return _to_base(quantity, unit)
