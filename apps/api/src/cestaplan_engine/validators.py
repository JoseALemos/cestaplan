"""Hard and soft constraint validators (OPTIMIZATION.md §2.3, §2.4).

Allergens are a HARD, non-relaxable safety constraint: a recipe whose declared
(or catalog-derived) allergens intersect any member's allergens is rejected.
Missing allergen data is treated conservatively — we warn rather than assume
"safe". Dietary restrictions may be hard (discard) or soft (score penalty).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cestaplan_engine.contracts import (
    CandidateRecipeDTO,
    CatalogProductDTO,
    MemberDTO,
)

# Dietary restrictions that forbid whole ingredient classes. Values are the
# allergen/tag tokens (lowercased) that, if present, make a recipe non-compliant.
_RESTRICTION_FORBIDDEN: dict[str, set[str]] = {
    "vegan": {"meat", "fish", "shellfish", "milk", "dairy", "egg", "eggs", "honey"},
    "vegetarian": {"meat", "fish", "shellfish", "poultry"},
    "gluten_free": {"gluten", "wheat", "barley", "rye"},
    "lactose_free": {"lactose", "milk", "dairy"},
    "pescatarian": {"meat", "poultry"},
    "halal": {"pork", "alcohol"},
    "kosher": {"pork", "shellfish"},
}


@dataclass
class ValidationResult:
    """Outcome of validating one recipe against the household."""

    valid: bool
    hard_violations: list[str] = field(default_factory=list)
    soft_violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _lower_set(values: set[str]) -> set[str]:
    return {v.strip().lower() for v in values if v.strip()}


class AllergenValidator:
    """HARD safety gate: reject any recipe unsafe for any member (OPTIMIZATION.md §2.3)."""

    def __init__(self, catalog: list[CatalogProductDTO] | None = None) -> None:
        # canonical_name -> derived allergen set from the product catalog.
        self._by_ingredient: dict[str, set[str]] = {}
        for product in catalog or []:
            self._by_ingredient.setdefault(product.canonical_name, set()).update(
                _lower_set(product.allergens)
            )

    def derived_allergens(self, recipe: CandidateRecipeDTO) -> set[str]:
        """Declared allergens plus those derived from the recipe's ingredients."""
        allergens = _lower_set(recipe.allergens_declared)
        for ing in recipe.ingredients:
            allergens |= self._by_ingredient.get(ing.canonical_name, set())
        return allergens

    def validate(
        self, recipe: CandidateRecipeDTO, members: list[MemberDTO]
    ) -> ValidationResult:
        household_allergens = set()
        for m in members:
            household_allergens |= _lower_set(m.allergens)
        if not household_allergens:
            return ValidationResult(valid=True)

        recipe_allergens = self.derived_allergens(recipe)
        conflict = recipe_allergens & household_allergens
        result = ValidationResult(valid=not conflict)
        for allergen in sorted(conflict):
            result.hard_violations.append(
                f"allergen:{allergen} in recipe '{recipe.title}'"
            )

        # Conservative warning: recipe declares no allergen data at all.
        declared_any = bool(recipe.allergens_declared) or any(
            ing.canonical_name in self._by_ingredient for ing in recipe.ingredients
        )
        if not declared_any:
            result.warnings.append(
                f"recipe '{recipe.title}' has no allergen data; verify product labels"
            )
        return result


class DietaryRestrictionValidator:
    """Applies dietary restrictions: hard ones discard, soft ones penalize (§2.4)."""

    def __init__(self, catalog: list[CatalogProductDTO] | None = None) -> None:
        self._by_ingredient: dict[str, set[str]] = {}
        for product in catalog or []:
            self._by_ingredient.setdefault(product.canonical_name, set()).update(
                _lower_set(product.allergens)
            )

    def _recipe_tokens(self, recipe: CandidateRecipeDTO) -> set[str]:
        tokens = _lower_set(set(recipe.preference_tags))
        tokens |= _lower_set(recipe.allergens_declared)
        for ing in recipe.ingredients:
            tokens.add(ing.canonical_name.strip().lower())
            tokens |= self._by_ingredient.get(ing.canonical_name, set())
        return tokens

    def validate(
        self, recipe: CandidateRecipeDTO, members: list[MemberDTO]
    ) -> ValidationResult:
        result = ValidationResult(valid=True)
        tokens = self._recipe_tokens(recipe)

        for member in members:
            for restriction in _lower_set(member.hard_restrictions):
                forbidden = _RESTRICTION_FORBIDDEN.get(restriction)
                if forbidden is not None:
                    hit = tokens & forbidden
                    if hit:
                        result.valid = False
                        result.hard_violations.append(
                            f"restriction:{restriction} ({', '.join(sorted(hit))}) "
                            f"for {member.alias}"
                        )
                elif restriction in tokens:
                    # Free-form excluded ingredient given directly as a token.
                    result.valid = False
                    result.hard_violations.append(
                        f"restriction:{restriction} for {member.alias}"
                    )

            for pref in member.soft_preferences:
                token = pref.strip().lower()
                if token.startswith("avoid:"):
                    avoided = token.split(":", 1)[1]
                    if avoided and avoided in tokens:
                        result.soft_violations.append(
                            f"soft:{avoided} disliked by {member.alias}"
                        )
        return result
