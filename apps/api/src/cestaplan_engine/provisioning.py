"""Provisioning: turn chosen meals into grocery lines and cost (the costing core).

Given a set of scheduled meals, aggregate ingredient demand per product, subtract
pantry, buy whole packages (:mod:`packaging`) and price them (splitting known vs
estimated). This is the single source of truth for cost, shared by the optimizer
(to evaluate candidate plans) and the facade (to build the final result).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from cestaplan_engine.contracts import (
    CandidateRecipeDTO,
    CatalogProductDTO,
    GroceryLineDTO,
    LeftoverDTO,
    PantryUsedDTO,
    ScoringWeights,
)
from cestaplan_engine.matching import ProductMatcher
from cestaplan_engine.packaging import PackageOptimizer
from cestaplan_engine.pantry import PantryCalculator
from cestaplan_engine.units import ConversionError, UnitConverter


@dataclass(frozen=True)
class MealAssignment:
    """A recipe placed on a slot, with the servings it must cover."""

    slot_index: int
    date: date
    meal_type: str
    recipe: CandidateRecipeDTO
    servings: int
    participants: tuple[str, ...]


@dataclass
class Provision:
    """Result of provisioning a set of meals."""

    grocery_lines: list[GroceryLineDTO] = field(default_factory=list)
    leftovers: list[LeftoverDTO] = field(default_factory=list)
    pantry_used: list[PantryUsedDTO] = field(default_factory=list)
    cost_known: Decimal = Decimal("0")
    cost_estimated: Decimal = Decimal("0")
    imputable_by_meal: dict[int, Decimal] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def cost_total(self) -> Decimal:
        return self.cost_known + self.cost_estimated


class Provisioner:
    """Aggregates demand and computes whole-package grocery lines + cost."""

    def __init__(
        self,
        matcher: ProductMatcher,
        pantry: PantryCalculator,
        package_optimizer: PackageOptimizer,
        weights: ScoringWeights,
        converter: UnitConverter,
        as_of: date | None,
    ) -> None:
        self._matcher = matcher
        self._pantry = pantry
        self._packager = package_optimizer
        self._weights = weights
        self._converter = converter
        self._as_of = as_of

    @staticmethod
    def _target_unit(product: CatalogProductDTO, fallback: str) -> str:
        if product.packages:
            return product.packages[0].package_unit
        return fallback

    def provision(self, meals: list[MealAssignment]) -> Provision:
        prov = Provision()

        # line_key -> (canonical_name, product, target_unit, category, display_name)
        line_meta: dict[str, tuple] = {}
        demand: dict[str, Decimal] = {}
        # (line_key, slot_index) -> demand contributed by that meal (target unit).
        demand_by_meal: dict[tuple[str, int], Decimal] = {}
        seen_warn: set[str] = set()

        for meal in meals:
            recipe = meal.recipe
            scale = Decimal(meal.servings) / Decimal(recipe.servings)
            for ing in recipe.ingredients:
                product = self._matcher.match_ingredient(ing)
                canonical = ing.canonical_name
                if product is not None:
                    key = f"p:{product.product_id}"
                    target_unit = self._target_unit(product, ing.unit)
                    category = product.category
                    display = product.display_name
                else:
                    key = f"i:{canonical}"
                    target_unit = ing.unit
                    category = "uncategorized"
                    display = ing.display_name

                raw_qty = ing.quantity * scale
                try:
                    qty = self._converter.convert(
                        raw_qty, ing.unit, target_unit, canonical
                    )
                except ConversionError:
                    if key not in seen_warn:
                        prov.warnings.append(
                            f"cannot convert {ing.unit!r} -> {target_unit!r} for "
                            f"{canonical!r}; line left without price"
                        )
                        seen_warn.add(key)
                    # Record as an unmatched (price-less) line keyed by ingredient.
                    key = f"i:{canonical}"
                    target_unit = ing.unit
                    qty = raw_qty
                    product = None
                    category = "uncategorized"
                    display = ing.display_name

                line_meta.setdefault(
                    key, (canonical, product, target_unit, category, display)
                )
                demand[key] = demand.get(key, Decimal("0")) + qty
                mk = (key, meal.slot_index)
                demand_by_meal[mk] = demand_by_meal.get(mk, Decimal("0")) + qty

        for key in sorted(demand):
            canonical, product, target_unit, category, display = line_meta[key]
            needed = demand[key]

            if product is None or not product.packages:
                # No product / no packages -> price-less line (hurts coverage).
                prov.grocery_lines.append(
                    GroceryLineDTO(
                        canonical_name=canonical,
                        product_id=(product.product_id if product else None),
                        display_name=display,
                        category=category,
                        needed_quantity=needed,
                        pending_quantity=needed,
                        used_quantity=needed,
                        package_unit=target_unit,
                        subtotal=Decimal("0"),
                        subtotal_known=False,
                    )
                )
                continue

            pantry_used, pending = self._pantry.pending(canonical, needed, target_unit)
            choice = self._packager.choose(
                pending,
                product.packages,
                as_of=self._as_of,
                w_waste=self._weights.waste,
                w_cost=self._weights.cost,
            )
            if choice is None:
                prov.grocery_lines.append(
                    GroceryLineDTO(
                        canonical_name=canonical,
                        product_id=product.product_id,
                        display_name=display,
                        category=category,
                        needed_quantity=needed,
                        pantry_quantity=pantry_used,
                        pending_quantity=pending,
                        used_quantity=needed,
                        package_unit=target_unit,
                        subtotal=Decimal("0"),
                        subtotal_known=False,
                    )
                )
                continue

            res = choice.result
            opt = choice.option
            subtotal = res.total_cost
            if choice.price_known:
                prov.cost_known += subtotal
            else:
                prov.cost_estimated += subtotal

            leftover = res.leftover
            prov.grocery_lines.append(
                GroceryLineDTO(
                    canonical_name=canonical,
                    product_id=product.product_id,
                    display_name=display,
                    category=category,
                    needed_quantity=needed,
                    pantry_quantity=pantry_used,
                    pending_quantity=pending,
                    packages_count=res.packages,
                    package_quantity=opt.package_quantity,
                    package_unit=opt.package_unit,
                    package_price=opt.amount,
                    purchased_quantity=res.purchased,
                    used_quantity=needed,
                    leftover=leftover,
                    subtotal=subtotal,
                    subtotal_known=choice.price_known,
                    availability=opt.availability,
                    source_type=opt.source_type,
                    source_name=opt.source_name,
                    observed_at=opt.observed_at,
                    expired=choice.expired,
                )
            )

            if pantry_used > 0:
                prov.pantry_used.append(
                    PantryUsedDTO(
                        canonical_name=canonical,
                        quantity=pantry_used,
                        unit=target_unit,
                    )
                )
            if leftover > 0:
                prov.leftovers.append(
                    LeftoverDTO(
                        canonical_name=canonical,
                        product_id=product.product_id,
                        display_name=display,
                        quantity=leftover,
                        unit=opt.package_unit,
                    )
                )

            # Allocate this line's subtotal to meals by demand share (imputable cost).
            if needed > 0 and subtotal > 0:
                for meal in meals:
                    share_qty = demand_by_meal.get((key, meal.slot_index))
                    if share_qty:
                        alloc = subtotal * share_qty / needed
                        prov.imputable_by_meal[meal.slot_index] = (
                            prov.imputable_by_meal.get(meal.slot_index, Decimal("0"))
                            + alloc
                        )

        return prov
