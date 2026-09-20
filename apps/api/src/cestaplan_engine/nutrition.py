"""Nutrition from used quantities (OPTIMIZATION.md §2.8).

Computes per-meal nutrition from the grams actually used and each product's
declared per-100 g nutrition. If a product lacks nutrition data or its used
quantity cannot be converted to grams, the meal is flagged as nutrition
``incomplete`` — the engine never invents macros.
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_engine.contracts import NutritionDTO
from cestaplan_engine.matching import ProductMatcher
from cestaplan_engine.provisioning import MealAssignment
from cestaplan_engine.units import ConversionError, UnitConverter

_MACROS = ("kcal", "protein_g", "carbs_g", "fat_g")


class NutritionCalculator:
    """Sums nutrition for a meal from its ingredients' used grams."""

    def __init__(self, matcher: ProductMatcher, converter: UnitConverter) -> None:
        self._matcher = matcher
        self._converter = converter

    def _accumulate(
        self, recipe, scale: Decimal, totals: dict[str, Decimal]
    ) -> tuple[bool, bool]:
        """Add ``recipe``'s nutrition at ``scale`` into ``totals``.

        Returns ``(complete, contributed)``: ``complete`` is ``False`` if any macro
        or ingredient could not be resolved; ``contributed`` is ``True`` if at least
        one ingredient added nutrition.
        """
        complete = True
        contributed = False
        for ing in recipe.ingredients:
            product = self._matcher.match_ingredient(ing)
            if product is None or product.nutrition is None:
                complete = False
                continue
            try:
                grams = self._converter.convert(
                    ing.quantity * scale, ing.unit, "g", ing.canonical_name
                )
            except ConversionError:
                complete = False
                continue
            factor = grams / Decimal("100")
            nut = product.nutrition
            for macro in _MACROS:
                value = getattr(nut, macro)
                if value is None:
                    complete = False
                    continue
                totals[macro] += value * factor
            contributed = True
        return complete, contributed

    def for_meal(self, meal: MealAssignment) -> tuple[NutritionDTO | None, bool]:
        """Return ``(nutrition, complete)`` for one meal's full cooked batch."""
        recipe = meal.recipe
        scale = Decimal(meal.servings) / Decimal(recipe.servings)
        totals: dict[str, Decimal] = {m: Decimal("0") for m in _MACROS}
        complete, contributed = self._accumulate(recipe, scale, totals)
        if not contributed:
            return None, False
        return (
            NutritionDTO(
                kcal=totals["kcal"],
                protein_g=totals["protein_g"],
                carbs_g=totals["carbs_g"],
                fat_g=totals["fat_g"],
            ),
            complete,
        )

    def for_meals_per_serving(
        self, meals: list[MealAssignment]
    ) -> tuple[dict[str, Decimal], bool]:
        """Sum each macro across meals counting ONE standard serving per meal.

        Batch-size independent: a meal cooked in 4 servings contributes the same as
        one cooked in 2, because nutrition targets are compared against the number of
        eaters (``comensales``), not how many portions are batch-cooked for leftovers.
        The caller scales this per-serving total by the household's eater weight.

        Returns ``(totals, complete)`` where ``complete`` is ``True`` only if every
        meal's nutrition could be fully computed.
        """
        totals: dict[str, Decimal] = {m: Decimal("0") for m in _MACROS}
        complete = True
        for meal in meals:
            scale = Decimal("1") / Decimal(meal.recipe.servings)
            meal_totals: dict[str, Decimal] = {m: Decimal("0") for m in _MACROS}
            meal_complete, contributed = self._accumulate(
                meal.recipe, scale, meal_totals
            )
            if not meal_complete:
                complete = False
            if contributed:
                for macro in _MACROS:
                    totals[macro] += meal_totals[macro]
        return totals, complete
