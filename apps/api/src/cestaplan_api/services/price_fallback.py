"""Deterministic, explainable price fallback for a recipe ingredient (spec §Y).

When an ingredient cannot be costed with its original product, this walks a FIXED, ordered
ladder and records exactly what it did and why. It is a PURE function over pre-fetched
candidates (no DB, no network), so it is fully testable and reproducible.

Hard rules (never violated):
- never a zero price, never assumed availability, never substitute across an allergy;
- never silently change store/scope (a scope change is surfaced in the decision);
- staging data is never used for a production decision (the caller passes production candidates);
- a fixed package is never costed as a fractional amount unless its net content is known;
- a budget is never declared met with unpriced products.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from cestaplan_api.ingestion.providers.contracts import ProductCostingMode


class FallbackAction(StrEnum):
    ALTERNATE_VARIANT = "alternate_variant"
    ALTERNATE_BRAND = "alternate_brand"
    ALTERNATE_PACKAGE = "alternate_package"
    CANONICAL_EQUIVALENT = "canonical_equivalent"
    DIETARY_SUBSTITUTION = "dietary_substitution"
    RECIPE_REGENERATION_REQUIRED = "recipe_regeneration_required"
    PARTIAL_COST = "partial_cost"
    USER_APPROVAL_REQUIRED = "user_approval_required"
    NO_VERIFIED_SOLUTION = "no_verified_solution"


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass(slots=True)
class FallbackCandidate:
    """A pre-fetched costing candidate for an ingredient (production or evaluation scope)."""

    product_id: int
    variant_id: int | None
    canonical_name: str
    brand: str | None
    costing_mode: ProductCostingMode
    price: Decimal | None
    price_scope: str
    freshness: Freshness = Freshness.FRESH
    is_community: bool = False
    is_estimated: bool = False
    net_content_signature: str | None = None  # package size fingerprint, to detect a different one
    allergens: frozenset[str] = frozenset()
    diet_tags: frozenset[str] = frozenset()
    # kind of relation to the ingredient: same canonical product, a substitution, etc.
    same_canonical: bool = True
    is_substitution: bool = False


@dataclass(slots=True)
class IngredientNeed:
    """What the recipe needs and the hard constraints that gate any solution."""

    ingredient_id: int
    canonical_name: str
    quantity: Decimal
    unit: str
    required_scope: str
    household_allergens: frozenset[str] = frozenset()
    dietary_constraints: frozenset[str] = frozenset()  # forbidden diet tags
    original_product_id: int | None = None
    original_net_content_signature: str | None = None
    original_brand: str | None = None
    original_cost: Decimal | None = None


@dataclass(slots=True)
class FallbackDecision:
    ingredient_id: int
    action: FallbackAction
    reason: str
    hard_constraints_checked: bool = True
    allergens_checked: bool = True
    dietary_constraints_checked: bool = True
    price_scope: str | None = None
    original_product_id: int | None = None
    selected_product_id: int | None = None
    original_cost: Decimal | None = None
    replacement_cost: Decimal | None = None
    confidence: Decimal = Decimal("0")
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ingredient_id": self.ingredient_id,
            "action": self.action.value,
            "reason": self.reason,
            "hard_constraints_checked": self.hard_constraints_checked,
            "allergens_checked": self.allergens_checked,
            "dietary_constraints_checked": self.dietary_constraints_checked,
            "price_scope": self.price_scope,
            "original_product_id": self.original_product_id,
            "selected_product_id": self.selected_product_id,
            "original_cost": None if self.original_cost is None else str(self.original_cost),
            "replacement_cost": None
            if self.replacement_cost is None
            else str(self.replacement_cost),
            "confidence": str(self.confidence),
            "warnings": list(self.warnings),
        }


_SCOPE_COMPATIBLE = {  # a candidate scope is acceptable for a required scope if it is >= as broad
    "exact_store": 1,
    "delivery_zone": 2,
    "postal_code": 3,
    "municipality": 4,
    "province": 5,
    "region": 6,
    "national": 7,
    "unknown": 8,
}


def _scope_ok(candidate_scope: str, required_scope: str) -> bool:
    """A candidate must be at least as specific as required (never a broader-than-asked scope
    silently upgraded); ``unknown`` is only acceptable when the requirement is also unknown."""
    if candidate_scope == "unknown":
        return required_scope == "unknown"
    return _SCOPE_COMPATIBLE.get(candidate_scope, 99) <= _SCOPE_COMPATIBLE.get(required_scope, 0)


def _blocks_on_constraint(c: FallbackCandidate, need: IngredientNeed) -> bool:
    """True when a hard constraint (allergen or diet) forbids this candidate — never overridden."""
    if c.allergens & need.household_allergens:
        return True
    return bool(c.diet_tags & need.dietary_constraints)


def _usable(
    c: FallbackCandidate,
    need: IngredientNeed,
    *,
    allow_stale: bool,
    allow_estimated: bool,
    allow_community: bool,
) -> bool:
    """A candidate is directly usable: real price, resolvable costing, compatible scope, and
    fresh/first-party — unless the caller has explicitly authorised stale/community/estimated."""
    if c.price is None or c.price <= 0:  # never a zero/negative price
        return False
    if c.costing_mode is ProductCostingMode.UNRESOLVED:
        return False
    if _blocks_on_constraint(c, need):  # allergen/diet is never overridden
        return False
    if not _scope_ok(c.price_scope, need.required_scope):
        return False
    if c.freshness is not Freshness.FRESH and not allow_stale:
        return False
    if c.is_community and not allow_community:
        return False
    return not (c.is_estimated and not allow_estimated)


def _decide(
    need: IngredientNeed,
    candidate: FallbackCandidate,
    action: FallbackAction,
    reason: str,
    *,
    confidence: Decimal,
) -> FallbackDecision:
    warnings: list[str] = []
    if candidate.price_scope != need.required_scope:
        warnings.append(
            f"scope changed {need.required_scope} -> {candidate.price_scope} (surfaced, not silent)"
        )
    if candidate.brand and action is FallbackAction.ALTERNATE_BRAND:
        warnings.append(f"brand substituted -> {candidate.brand}")
    return FallbackDecision(
        ingredient_id=need.ingredient_id,
        action=action,
        reason=reason,
        price_scope=candidate.price_scope,
        original_product_id=need.original_product_id,
        selected_product_id=candidate.product_id,
        original_cost=need.original_cost,
        replacement_cost=candidate.price,
        confidence=confidence,
        warnings=warnings,
    )


def resolve_with_fallback(
    need: IngredientNeed,
    candidates: list[FallbackCandidate],
    *,
    allow_stale: bool = False,
    allow_estimated: bool = False,
    allow_community: bool = False,
) -> FallbackDecision:
    """Walk the deterministic 9-step ladder and return the single decision taken.

    Steps 1-5 look for a directly-usable candidate (real price, resolvable, in-scope, fresh, no
    constraint breach). Steps 6-9 degrade honestly: regenerate the recipe, cost partially, ask
    the user to approve an old/community/estimated price, or declare no verifiable solution.
    """
    usable = [
        c
        for c in candidates
        if _usable(
            c, need, allow_stale=allow_stale, allow_estimated=allow_estimated,
            allow_community=allow_community,
        )
    ]

    def _diff_pkg(c: FallbackCandidate) -> bool:
        return c.net_content_signature != need.original_net_content_signature

    # 1. another variant of the SAME product, same scope.
    for c in usable:
        if c.product_id == need.original_product_id and c.variant_id is not None:
            return _decide(
                need, c, FallbackAction.ALTERNATE_VARIANT,
                "another variant of the same product in the same scope", confidence=Decimal("0.95"),
            )
    # 2. a DIFFERENT package size of the same ingredient.
    for c in usable:
        if c.same_canonical and _diff_pkg(c):
            return _decide(
                need, c, FallbackAction.ALTERNATE_PACKAGE,
                "a different package size of the same ingredient", confidence=Decimal("0.85"),
            )
    # 3. a different BRAND (same ingredient, same package, brand differs from the original).
    for c in usable:
        if c.same_canonical and c.brand is not None and c.brand != need.original_brand:
            return _decide(
                need, c, FallbackAction.ALTERNATE_BRAND,
                "a different brand of the same ingredient", confidence=Decimal("0.8"),
            )
    # 4. another product mapped to the SAME ingredient (same package, brand not the differentiator).
    for c in usable:
        if c.same_canonical and c.product_id != need.original_product_id:
            return _decide(
                need, c, FallbackAction.CANONICAL_EQUIVALENT,
                "another product mapped to the same ingredient", confidence=Decimal("0.9"),
            )
    # 5. a compatible dietary/food substitution (already passed allergens + diet in _usable).
    for c in usable:
        if c.is_substitution:
            return _decide(
                need, c, FallbackAction.DIETARY_SUBSTITUTION,
                "a compatible food substitution (allergen/diet safe)", confidence=Decimal("0.7"),
            )

    # --- honest degradations (no in-policy usable candidate) --------------------------------- #
    # Constraint-safe priced candidates only — an allergen/diet breach is NEVER costed at all.
    priced_safe = [
        c for c in candidates
        if c.price is not None and c.price > 0 and not _blocks_on_constraint(c, need)
    ]

    # 8. a costable, constraint-safe candidate exists but only old/community/estimated -> approval.
    for c in priced_safe:
        if c.costing_mode is ProductCostingMode.UNRESOLVED:
            continue
        if (
            (c.freshness is not Freshness.FRESH and not allow_stale)
            or (c.is_community and not allow_community)
            or (c.is_estimated and not allow_estimated)
        ):
            d = FallbackDecision(
                ingredient_id=need.ingredient_id,
                action=FallbackAction.USER_APPROVAL_REQUIRED,
                reason="only an old/community/estimated price is available",
                price_scope=c.price_scope,
                original_product_id=need.original_product_id,
                selected_product_id=c.product_id,
                original_cost=need.original_cost,
                replacement_cost=c.price,
                confidence=Decimal("0.4"),
            )
            d.warnings.append("requires explicit user approval before use")
            return d

    # 7. a priced, constraint-safe candidate exists but cannot be cleanly costed (scope/package).
    if priced_safe:
        d = FallbackDecision(
            ingredient_id=need.ingredient_id,
            action=FallbackAction.PARTIAL_COST,
            reason="a priced candidate exists but its costing/scope is unresolved",
            original_product_id=need.original_product_id,
            confidence=Decimal("0.3"),
        )
        d.warnings.append("recipe cost is PARTIAL for this ingredient")
        return d

    # 9. priced products exist but ALL breach a hard constraint -> never substitute an allergy/diet.
    if any(c.price is not None and c.price > 0 for c in candidates):
        return FallbackDecision(
            ingredient_id=need.ingredient_id,
            action=FallbackAction.NO_VERIFIED_SOLUTION,
            reason="only constraint-breaching (allergen/diet) products exist — never substituted",
            original_product_id=need.original_product_id,
            confidence=Decimal("0"),
            warnings=["no verifiable solution — ingredient left uncosted"],
        )

    # 6. candidates exist but none is priced -> a different recipe/ingredient set is required.
    if candidates:
        return FallbackDecision(
            ingredient_id=need.ingredient_id,
            action=FallbackAction.RECIPE_REGENERATION_REQUIRED,
            reason="no priced candidate; a different recipe/ingredient set is required",
            original_product_id=need.original_product_id,
            confidence=Decimal("0.1"),
        )

    # 9. nothing at all.
    return FallbackDecision(
        ingredient_id=need.ingredient_id,
        action=FallbackAction.NO_VERIFIED_SOLUTION,
        reason="no mapped, priced, in-scope and constraint-safe product exists",
        original_product_id=need.original_product_id,
        confidence=Decimal("0"),
        warnings=["no verifiable solution — ingredient left uncosted"],
    )


__all__ = [
    "FallbackAction",
    "FallbackCandidate",
    "FallbackDecision",
    "Freshness",
    "IngredientNeed",
    "resolve_with_fallback",
]
