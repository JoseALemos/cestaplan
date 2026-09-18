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
# allergen/tag/category tokens (lowercased) that, if present, make a recipe non-compliant.
_RESTRICTION_FORBIDDEN: dict[str, set[str]] = {
    "vegan": {"meat", "poultry", "fish", "shellfish", "milk", "dairy", "egg", "eggs", "honey"},
    "vegetarian": {"meat", "fish", "shellfish", "poultry"},
    "gluten_free": {"gluten", "wheat", "barley", "rye"},
    "lactose_free": {"lactose", "milk", "dairy"},
    "pescatarian": {"meat", "poultry"},
    "halal": {"pork", "alcohol"},
    "kosher": {"pork", "shellfish"},
}

# The UI (and real households) send diet types in Spanish; the restriction table above keys on
# canonical English. Without this bridge ``_RESTRICTION_FORBIDDEN.get("vegano")`` is ``None`` and
# the whole diet becomes a silent no-op. Maps to "" for "no restriction" (omnívoro). English
# values pass through unchanged.
_DIET_ALIASES: dict[str, str] = {
    "vegano": "vegan",
    "vegana": "vegan",
    "vegetariano": "vegetarian",
    "vegetariana": "vegetarian",
    "pescetariano": "pescatarian",
    "pescetariana": "pescatarian",
    "pescatariano": "pescatarian",
    "sin_gluten": "gluten_free",
    "sin gluten": "gluten_free",
    "sin_lactosa": "lactose_free",
    "sin lactosa": "lactose_free",
    "omnivoro": "",
    "omnívoro": "",
}

# Ingredient category_code -> diet tokens it contributes. Meat/poultry, fish/shellfish, eggs and
# dairy are excluded by diet through the ingredient's CATEGORY, because the recipe's ingredient
# tokens are canonical names ("pollo_pechuga", "cerdo_lomo") that never equal the generic "meat".
# ("carne" holds poultry and pork too; the seed does not split them finer, so it maps to both meat
# and poultry — enough for vegan/vegetarian/pescatarian, which forbid whole classes.)
_CATEGORY_FORBIDS: dict[str, set[str]] = {
    "carne": {"meat", "poultry"},
    "pescado_marisco": {"fish", "shellfish"},
    "huevos": {"egg", "eggs"},
    "lacteos": {"milk", "dairy"},
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


# The UI emits EU-14 allergen codes in the plural English form (crustaceans, eggs, peanuts,
# soybeans, nuts, sulphites, molluscs) while the ingredient catalogue tags them singular
# (crustacean, egg, peanut, soy, tree_nut, sulphite, mollusc). Left unbridged, a declared allergy
# silently fails to match the recipe — a safety bug. Canonicalize BOTH the household's and the
# recipe's codes to one set (the singular catalogue form) before intersecting. A few common Spanish
# and synonym spellings are tolerated defensively (the field is free text at the API).
_ALLERGEN_CANON: dict[str, str] = {
    "crustaceans": "crustacean", "crustaceos": "crustacean", "crustáceos": "crustacean",
    "molluscs": "mollusc", "moluscos": "mollusc",
    "eggs": "egg", "huevo": "egg", "huevos": "egg",
    "peanuts": "peanut", "cacahuete": "peanut", "cacahuetes": "peanut", "mani": "peanut",
    "soybeans": "soy", "soybean": "soy", "soya": "soy", "soja": "soy",
    "nuts": "tree_nut", "tree_nuts": "tree_nut", "frutos_secos": "tree_nut",
    "frutos_de_cascara": "tree_nut", "frutos de cáscara": "tree_nut",
    "sulphites": "sulphite", "sulfites": "sulphite", "sulfito": "sulphite", "sulfitos": "sulphite",
    "milk": "milk", "lactose": "milk", "dairy": "milk", "leche": "milk", "lactosa": "milk",
    "gluten": "gluten", "wheat": "gluten", "trigo": "gluten",
    "fish": "fish", "pescado": "fish",
    "sesame": "sesame", "sesamo": "sesame", "sésamo": "sesame",
    "celery": "celery", "apio": "celery",
    "mustard": "mustard", "mostaza": "mustard",
    "lupin": "lupin", "lupins": "lupin", "altramuces": "lupin",
}


def _canon_allergens(values: set[str]) -> set[str]:
    """Lowercase, trim and map each allergen code to its canonical catalogue form."""
    out: set[str] = set()
    for value in values:
        code = value.strip().lower()
        if code:
            out.add(_ALLERGEN_CANON.get(code, code))
    return out


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
        """Declared allergens plus those derived from the recipe's ingredients (canonicalized)."""
        allergens = _lower_set(recipe.allergens_declared)
        for ing in recipe.ingredients:
            allergens |= self._by_ingredient.get(ing.canonical_name, set())
        return _canon_allergens(allergens)

    def validate(
        self, recipe: CandidateRecipeDTO, members: list[MemberDTO]
    ) -> ValidationResult:
        household_allergens: set[str] = set()
        strict: set[str] = set()
        for m in members:
            household_allergens |= _canon_allergens(m.allergens)
            strict |= _canon_allergens(m.strict_allergen_codes)

        result = ValidationResult(valid=True)

        # 1) Direct conflict: a declared allergen is present in the recipe (declared or derived).
        if household_allergens:
            conflict = self.derived_allergens(recipe) & household_allergens
            if conflict:
                result.valid = False
                for allergen in sorted(conflict):
                    result.hard_violations.append(
                        f"allergen:{allergen} in recipe '{recipe.title}'"
                    )

        # 2) Fail-closed: a member with a SERIOUS allergy (allergy/anaphylaxis, not intolerance)
        # cannot be served a recipe containing any ingredient whose allergen profile is UNKNOWN
        # (unassessed). Better an infeasible plan than an unsafe one.
        if strict:
            unassessed = sorted(
                ing.canonical_name for ing in recipe.ingredients if not ing.allergen_assessed
            )
            if unassessed:
                result.valid = False
                shown = ", ".join(unassessed[:3]) + ("…" if len(unassessed) > 3 else "")
                result.hard_violations.append(
                    f"allergen_unassessed:{shown} (serious allergy; safety cannot be guaranteed) "
                    f"in recipe '{recipe.title}'"
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
            if ing.category:
                tokens |= _CATEGORY_FORBIDS.get(ing.category.strip().lower(), set())
        return tokens

    def validate(
        self, recipe: CandidateRecipeDTO, members: list[MemberDTO]
    ) -> ValidationResult:
        result = ValidationResult(valid=True)
        tokens = self._recipe_tokens(recipe)

        for member in members:
            for restriction in _lower_set(member.hard_restrictions):
                # Bridge Spanish diet names to canonical English before the lookup; "" (omnívoro)
                # falls through as "no dietary class", leaving free-form ingredient exclusions.
                diet = _DIET_ALIASES.get(restriction, restriction)
                forbidden = _RESTRICTION_FORBIDDEN.get(diet)
                if forbidden is not None:
                    hit = tokens & forbidden
                    if hit:
                        result.valid = False
                        result.hard_violations.append(
                            f"restriction:{diet} ({', '.join(sorted(hit))}) "
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
