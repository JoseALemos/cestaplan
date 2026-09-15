"""Multi-chain price comparison for a costed meal plan (PHASE 1: pure price, no travel).

Costs a plan's MANDATORY ingredient basket against every user-visible chain using the SAME
deterministic engine costing the live planner uses (whole-package maths + ``Decimal``), then
picks the cheapest single chain and computes the optimal cross-chain split. There is no
geolocation or travel cost in this phase — it is a pure price comparison.

Reuse (no reinvention): the per-retailer priced catalog + conversions come straight from
:mod:`cestaplan_api.services.planning_context` (``_build_catalog`` / ``_build_conversions`` —
the very builders the live planner feeds the engine), and the costing itself is the engine's
own :class:`~cestaplan_engine.provisioning.Provisioner` (aggregate demand -> subtract pantry ->
buy whole packages -> price), wired exactly like :func:`cestaplan_engine.facade.generate_plan`.

Honesty invariants (never violated):
* money is always :class:`~decimal.Decimal`, serialized to JSON as strings — never ``float``;
* a chain that does not price an ingredient simply omits it from that chain's basket (it lowers
  that chain's coverage), a price is NEVER invented;
* an ingredient no chain prices is reported as ``uncovered`` — never guessed.

Two deliberate differences from the plan's live cost, for a fair cross-chain comparison:
* only MANDATORY ingredients are costed (optionals are excluded, so a chain that cannot map an
  optional is not penalised vs one that can);
* an EMPTY pantry is used (a pure basket price; the household's stock — identical across chains —
  never tilts the comparison).

PHASE 2 (travel cost): :func:`compare_plan_with_travel` wraps :func:`compare_plan_across_chains`
(which stays pure-price, unchanged) and, only when the household has a geocoded address, adds a
per-chain travel cost (:mod:`cestaplan_api.services.geo.household_geo`) so "cheapest chain" and
"cheapest split" can be compared including the trip there. A chain with no address or no known
travel distance simply carries no travel numbers — never an invented one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import Household, MealPlan, PlannedMeal, ProductPrice, Recipe, Retailer
from cestaplan_api.services.geo.household_geo import travel_by_retailer
from cestaplan_api.services.planning_context import _build_catalog, _build_conversions
from cestaplan_engine.contracts import (
    CandidateRecipeDTO,
    CatalogProductDTO,
    CoverageDTO,
    IngredientConversionDTO,
    RecipeIngredientDTO,
    ScoringWeights,
)
from cestaplan_engine.matching import ProductMatcher
from cestaplan_engine.packaging import PackageOptimizer
from cestaplan_engine.pantry import PantryCalculator
from cestaplan_engine.pricing import compute_coverage
from cestaplan_engine.provisioning import MealAssignment, Provision, Provisioner
from cestaplan_engine.units import ConversionError, UnitConverter

# A chain must cover at least this share of the basket before its total is offered as a
# candidate "shop it all here" option when NO chain covers the basket in full. Below this a
# chain's total prices a materially smaller basket and is not comparable, so it is not a
# best_single candidate (its per-item prices still feed the cross-chain split). Assumption to
# confirm with product: 0.8 (near-full).
_NEAR_FULL_COVERAGE = Decimal("0.8")


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class _BasketItem:
    """One mandatory basket ingredient, consolidated across the plan (chain-independent)."""

    canonical_name: str
    display_name: str
    required_quantity: Decimal
    required_unit: str


@dataclass
class _ChainResult:
    """A single chain's costing of the whole mandatory basket."""

    retailer: Retailer
    known_cost: Decimal
    coverage: CoverageDTO
    # canonical_name -> whole-package cost of that ingredient's required quantity at this chain.
    # Only ingredients this chain actually prices appear here (never an invented price).
    costs: dict[str, Decimal]

    @property
    def ratio(self) -> Decimal:
        return self.coverage.price_coverage

    @property
    def full_coverage(self) -> bool:
        return self.coverage.price_coverage == Decimal("1")


# --------------------------------------------------------------------------- #
# Basket assembly (the plan's mandatory ingredients, engine-ready)
# --------------------------------------------------------------------------- #
def _mandatory_recipe_dto(recipe: Recipe) -> CandidateRecipeDTO:
    """A candidate DTO carrying ONLY the recipe's mandatory (non-optional) ingredients."""
    return CandidateRecipeDTO(
        recipe_id=str(recipe.id),
        title=recipe.title,
        servings=recipe.servings or 1,
        ingredients=[
            RecipeIngredientDTO(
                canonical_name=ri.canonical_name,
                display_name=ri.display_name or ri.canonical_name,
                quantity=Decimal(ri.quantity),
                unit=ri.unit,
                optional=False,
                substitution_group=ri.substitution_group,
            )
            for ri in recipe.ingredients
            if not ri.optional
        ],
    )


def _plan_meals(db: Session, meal_plan: MealPlan) -> list[MealAssignment]:
    """Every planned meal as a :class:`MealAssignment` over its mandatory ingredients.

    Servings and repeated recipes are preserved so demand aggregates exactly as it does live.
    """
    planned = (
        db.execute(
            select(PlannedMeal)
            .where(PlannedMeal.meal_plan_id == meal_plan.id)
            .order_by(PlannedMeal.id)
        )
        .scalars()
        .all()
    )
    recipe_ids = [pm.recipe_id for pm in planned]
    recipes = {
        r.id: r
        for r in db.execute(select(Recipe).where(Recipe.id.in_(recipe_ids))).scalars()
    }
    meals: list[MealAssignment] = []
    for index, pm in enumerate(planned):
        recipe = recipes.get(pm.recipe_id)
        if recipe is None:
            continue
        meals.append(
            MealAssignment(
                slot_index=index,
                date=pm.scheduled_date or meal_plan.start_date,
                meal_type=pm.meal_type,
                recipe=_mandatory_recipe_dto(recipe),
                servings=pm.servings,
                participants=(),
            )
        )
    return meals


def _substitution_groups(meals: list[MealAssignment]) -> dict[str, list[str]]:
    """Ordered substitution candidates per group (mirrors the facade's matcher wiring)."""
    groups: dict[str, list[str]] = {}
    for meal in meals:
        for ing in meal.recipe.ingredients:
            if ing.substitution_group:
                bucket = groups.setdefault(ing.substitution_group, [])
                if ing.canonical_name not in bucket:
                    bucket.append(ing.canonical_name)
    return groups


def _consolidate_basket(
    meals: list[MealAssignment], converter: UnitConverter
) -> list[_BasketItem]:
    """Consolidate the mandatory basket per canonical ingredient (chain-independent view).

    Quantities are summed in each ingredient's first-seen unit, converting later occurrences
    with the shared converter (so ``ml``/``g`` of the same ingredient combine via its density).
    Order follows first appearance in the plan for a deterministic response.
    """
    order: list[str] = []
    agg: dict[str, dict[str, Any]] = {}
    for meal in meals:
        scale = Decimal(meal.servings) / Decimal(meal.recipe.servings or 1)
        for ing in meal.recipe.ingredients:
            canonical = ing.canonical_name
            entry = agg.get(canonical)
            if entry is None:
                entry = {
                    "display_name": ing.display_name,
                    "quantity": Decimal("0"),
                    "unit": ing.unit,
                }
                agg[canonical] = entry
                order.append(canonical)
            qty = ing.quantity * scale
            try:
                qty = converter.convert(qty, ing.unit, entry["unit"], canonical)
            except ConversionError:
                if ing.unit != entry["unit"]:
                    # Incompatible units for the same ingredient: cannot safely combine.
                    continue
            entry["quantity"] += qty
    return [
        _BasketItem(
            canonical_name=canonical,
            display_name=agg[canonical]["display_name"],
            required_quantity=agg[canonical]["quantity"],
            required_unit=agg[canonical]["unit"],
        )
        for canonical in order
    ]


# --------------------------------------------------------------------------- #
# Per-chain costing (reuse the engine's own Provisioner, exactly like the facade)
# --------------------------------------------------------------------------- #
def _cost_basket(
    meals: list[MealAssignment],
    substitution_groups: dict[str, list[str]],
    catalog: list[CatalogProductDTO],
    conversions: list[IngredientConversionDTO],
    as_of: date,
) -> Provision:
    """Cost the plan's meals against one chain's catalog with the engine's whole-package maths.

    Wires the deterministic costing core the same way :func:`cestaplan_engine.facade.generate_plan`
    does — an EMPTY pantry (pure basket price), the default scoring weights — and returns the raw
    :class:`Provision` (grocery lines + known/estimated cost split).
    """
    converter = UnitConverter(conversions)
    matcher = ProductMatcher(catalog, substitution_groups)
    pantry = PantryCalculator([], converter, as_of)
    provisioner = Provisioner(
        matcher, pantry, PackageOptimizer(), ScoringWeights(), converter, as_of
    )
    return provisioner.provision(meals)


def _chain_costs(provision: Provision) -> dict[str, Decimal]:
    """canonical_name -> whole-package cost, only for ingredients this chain actually priced."""
    costs: dict[str, Decimal] = {}
    for line in provision.grocery_lines:
        if line.subtotal_known:
            costs.setdefault(line.canonical_name, line.subtotal)
    return costs


def _coverage_dict(coverage: CoverageDTO) -> dict[str, Any]:
    return {
        "with_price": coverage.counts.with_price,
        "without_price": coverage.counts.without_price,
        "ratio": _s(coverage.price_coverage),
        "status": coverage.status,
    }


# --------------------------------------------------------------------------- #
# best_single + cross-chain split
# --------------------------------------------------------------------------- #
def _best_single(results: list[_ChainResult]) -> _ChainResult | None:
    """Cheapest single chain to shop the whole basket at.

    Rule (assumption to confirm): prefer chains with COMPLETE coverage and, among them, the
    lowest total. If none is complete, fall back to chains covering at least
    ``_NEAR_FULL_COVERAGE`` of the basket, taking the most complete first and the cheapest to
    break ties (totals of unequal baskets are never compared as if equal). If not even a
    near-full chain exists, there is no defensible single-chain answer -> ``None``.
    """
    priced = [r for r in results if r.costs]
    if not priced:
        return None
    full = [r for r in priced if r.full_coverage]
    pool = full or [r for r in priced if r.ratio >= _NEAR_FULL_COVERAGE]
    if not pool:
        return None
    # Most complete first, then cheapest, then name for a deterministic tie-break.
    return min(pool, key=lambda r: (-r.ratio, r.known_cost, r.retailer.name))


def _split(
    basket: list[_BasketItem], results: list[_ChainResult]
) -> tuple[dict[int, list[dict[str, Any]]], Decimal, list[_BasketItem]]:
    """For each basket ingredient pick the cheapest chain that prices it.

    Returns ``(items_by_retailer_id, split_total, uncovered)``: which ingredients to buy at each
    chain (cheapest wins; ties broken by chain name), the split's whole-package total, and the
    ingredients no chain prices.
    """
    items_by_retailer: dict[int, list[dict[str, Any]]] = {}
    split_total = Decimal("0")
    uncovered: list[_BasketItem] = []
    for item in basket:
        offers = [
            (r.costs[item.canonical_name], r.retailer.name, r)
            for r in results
            if item.canonical_name in r.costs
        ]
        if not offers:
            uncovered.append(item)
            continue
        cost, _name, chosen = min(offers, key=lambda o: (o[0], o[1]))
        split_total += cost
        items_by_retailer.setdefault(chosen.retailer.id, []).append(
            {
                "canonical_name": item.canonical_name,
                "display_name": item.display_name,
                "cost": _s(cost),
                "required_quantity": _s(item.required_quantity),
                "required_unit": item.required_unit,
            }
        )
    return items_by_retailer, split_total, uncovered


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def compare_plan_across_chains(db: Session, meal_plan: MealPlan) -> dict[str, Any]:
    """Compare a plan's mandatory basket cost across every user-visible chain (money as strings).

    Enumerates the user-visible chains (active + at least one non-synthetic price — the visibility
    rule of ``catalog.list_retailers``, restricted to real prices so the demo catalogue never
    enters a real comparison), costs the basket against each with the live engine, then reports
    per-chain totals/coverage, the best single chain and the optimal cross-chain split.
    """
    as_of = date.today()
    meals = _plan_meals(db, meal_plan)
    # A shared converter (density conversions are chain-independent) drives basket consolidation.
    conversions = _build_conversions(db)
    substitution_groups = _substitution_groups(meals)
    basket = _consolidate_basket(meals, UnitConverter(conversions))

    results: list[_ChainResult] = []
    for retailer in _visible_chains(db):
        catalog = _build_catalog(db, retailer.id)
        provision = _cost_basket(meals, substitution_groups, catalog, conversions, as_of)
        results.append(
            _ChainResult(
                retailer=retailer,
                known_cost=provision.cost_known,
                coverage=compute_coverage(provision.grocery_lines),
                costs=_chain_costs(provision),
            )
        )

    best = _best_single(results)
    items_by_retailer, split_total, uncovered = _split(basket, results)
    savings = best.known_cost - split_total if best is not None else None

    retailer_name = {r.retailer.id: r.retailer.name for r in results}
    retailer_public = {r.retailer.id: str(r.retailer.public_id) for r in results}
    split_by_chain = [
        {
            "retailer_id": retailer_public[rid],
            "retailer_name": retailer_name[rid],
            "subtotal": _s(sum((Decimal(i["cost"]) for i in items), Decimal("0"))),
            "items": items,
        }
        for rid, items in sorted(items_by_retailer.items(), key=lambda kv: retailer_name[kv[0]])
    ]

    return {
        "meal_plan_id": str(meal_plan.public_id),
        "currency": meal_plan.currency,
        "basket": {
            "ingredient_count": len(basket),
            "ingredients": [
                {
                    "canonical_name": item.canonical_name,
                    "display_name": item.display_name,
                    "required_quantity": _s(item.required_quantity),
                    "required_unit": item.required_unit,
                }
                for item in basket
            ],
        },
        "chains": [
            {
                "retailer_id": str(r.retailer.public_id),
                "retailer_name": r.retailer.name,
                "known_cost": _s(r.known_cost),
                "full_coverage": r.full_coverage,
                "coverage": _coverage_dict(r.coverage),
                "ingredient_costs": {c: _s(v) for c, v in r.costs.items()},
            }
            for r in results
        ],
        "best_single": None
        if best is None
        else {
            "retailer_id": str(best.retailer.public_id),
            "retailer_name": best.retailer.name,
            "total": _s(best.known_cost),
            "coverage_ratio": _s(best.coverage.price_coverage),
            "full_coverage": best.full_coverage,
        },
        "split": {
            "total": _s(split_total),
            "distinct_chain_count": len(split_by_chain),
            "savings_vs_best_single": _s(savings),
            "by_chain": split_by_chain,
            "uncovered_ingredients": [
                {
                    "canonical_name": item.canonical_name,
                    "display_name": item.display_name,
                    "required_quantity": _s(item.required_quantity),
                    "required_unit": item.required_unit,
                }
                for item in uncovered
            ],
        },
    }


# --------------------------------------------------------------------------- #
# Public entrypoint (PHASE 2: price + travel cost)
# --------------------------------------------------------------------------- #
def _travel_dict(info: dict[str, Any]) -> dict[str, Any]:
    """Serialise one ``travel_by_retailer`` entry (money/coords as strings)."""
    nearest_store = info["nearest_store"]
    return {
        "distance_km": _s(info["distance_km"]),
        "travel_cost": _s(info["travel_cost"]),
        "nearest_store": None
        if nearest_store is None
        else {
            "name": nearest_store["name"],
            "latitude": _s(nearest_store["latitude"]),
            "longitude": _s(nearest_store["longitude"]),
        },
        "found": info["found"],
    }


def compare_plan_with_travel(
    db: Session,
    meal_plan: MealPlan,
    settings: Settings,
    household: Household | None,
) -> dict[str, Any]:
    """:func:`compare_plan_across_chains` plus travel cost when the household has an address.

    ``compare_plan_across_chains`` itself is never modified (stays pure-price); this wraps its
    result with a ``travel`` block and, per chain, a ``travel``/``total_with_travel`` addition —
    a chain whose distance/travel cost is unknown (no matching store found, no cache yet) simply
    carries no travel numbers rather than an invented one. Without a geocoded household address
    (or with ``settings.geo_enabled`` off) the base Phase-1 result is returned unchanged except
    for ``travel.has_address = False`` (the frontend then shows Phase-1 only).
    """
    base = compare_plan_across_chains(db, meal_plan)
    has_address = bool(
        household is not None
        and household.latitude is not None
        and household.longitude is not None
    )
    base["travel"] = {
        "enabled": settings.geo_enabled,
        "has_address": has_address,
        "rate_eur_per_km": _s(settings.travel_cost_eur_per_km),
        "detour_factor": _s(settings.travel_road_detour_factor),
    }
    if not settings.geo_enabled or not has_address:
        return base

    visible = _visible_chains(db)
    internal_id_by_public = {str(r.public_id): r.id for r in visible}
    travel_by_id = travel_by_retailer(db, household, visible, settings)

    for chain in base["chains"]:
        internal_id = internal_id_by_public.get(chain["retailer_id"])
        info = travel_by_id.get(internal_id) if internal_id is not None else None
        if info is None:
            info = {
                "distance_km": None,
                "travel_cost": None,
                "nearest_store": None,
                "found": False,
            }
        chain["travel"] = _travel_dict(info)
        travel_cost = info["travel_cost"]
        chain["total_with_travel"] = (
            None if travel_cost is None else _s(Decimal(chain["known_cost"]) + travel_cost)
        )

    # best_single_with_travel: same eligibility as best_single (full coverage, else near-full
    # ratio >= 0.8), restricted to chains whose travel cost is actually known.
    full_coverage_chains = [c for c in base["chains"] if c["full_coverage"]]
    eligible = full_coverage_chains or [
        c for c in base["chains"] if Decimal(c["coverage"]["ratio"]) >= _NEAR_FULL_COVERAGE
    ]
    priced_with_travel = [c for c in eligible if c["total_with_travel"] is not None]
    best_chain = (
        min(priced_with_travel, key=lambda c: (Decimal(c["total_with_travel"]), c["retailer_name"]))
        if priced_with_travel
        else None
    )
    base["best_single_with_travel"] = (
        None
        if best_chain is None
        else {
            "retailer_id": best_chain["retailer_id"],
            "retailer_name": best_chain["retailer_name"],
            "basket_cost": best_chain["known_cost"],
            "travel_cost": best_chain["travel"]["travel_cost"],
            "total_with_travel": best_chain["total_with_travel"],
        }
    )

    # Augment split: travel_total sums each DISTINCT chain the split actually visits. If any
    # visited chain's travel is unknown, the with-travel split figures are withheld rather than
    # silently omitting that chain's trip (never invents a distance, never understates a total).
    chain_travel_by_public_id = {c["retailer_id"]: c["travel"] for c in base["chains"]}
    split = base["split"]
    travel_total = Decimal("0")
    travel_known = True
    for entry in split["by_chain"]:
        travel_cost_str = chain_travel_by_public_id.get(entry["retailer_id"], {}).get("travel_cost")
        if travel_cost_str is None:
            travel_known = False
            break
        travel_total += Decimal(travel_cost_str)

    if travel_known:
        split["travel_total"] = _s(travel_total)
        split["total_with_travel"] = _s(Decimal(split["total"]) + travel_total)
    else:
        split["travel_total"] = None
        split["total_with_travel"] = None

    if base["best_single_with_travel"] is not None and split["total_with_travel"] is not None:
        split["savings_vs_best_single_with_travel"] = _s(
            Decimal(base["best_single_with_travel"]["total_with_travel"])
            - Decimal(split["total_with_travel"])
        )
    else:
        split["savings_vs_best_single_with_travel"] = None

    return base


def _visible_chains(db: Session) -> list[Retailer]:
    """Active chains with at least one NON-synthetic price.

    Same visibility rule as :func:`cestaplan_api.routers.catalog.list_retailers` (active +
    priced), restricted to real prices so the synthetic demo chain (MercaEjemplo) — whose prices
    are all synthetic — is excluded from a real cross-chain price comparison.
    """
    priced = (
        select(ProductPrice.retailer_id)
        .where(ProductPrice.is_synthetic.is_(False))
        .distinct()
        .scalar_subquery()
    )
    return list(
        db.execute(
            select(Retailer)
            .where(Retailer.is_active.is_(True), Retailer.id.in_(priced))
            .order_by(Retailer.name)
        )
        .scalars()
        .all()
    )


__all__ = ["compare_plan_across_chains", "compare_plan_with_travel"]
