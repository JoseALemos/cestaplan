"""Plan quality: variety + budget-as-envelope (FASE 3 follow-up).

Regression cover for the two real user complaints: (1) plans reused only ~3
recipes across 10 meals, and (2) plans landed far below budget by repeating the
single cheapest dish. The redesigned objective (optimizer._score) makes the
budget an envelope under the default "waste" priority and penalizes recipe reuse
superlinearly, so a rich candidate pool yields near-maximum distinct dishes.
"""

from __future__ import annotations

from collections import Counter

from cestaplan_engine import generate_plan
from cestaplan_engine.contracts import PlanResult

from .builders import ingredient, member, package, plan_input, product, recipe, requirement

# A 10-meal plan: 2 breakfast / 4 lunch / 1 snack / 3 dinner.
TEN_MEAL_REQS = [
    requirement("breakfast", 2),
    requirement("lunch", 4),
    requirement("snack", 1),
    requirement("dinner", 3),
]


def _solo(rid: str, meal_type: str, price: str):
    """A recipe with its OWN product, consuming exactly one whole package.

    Distinct product per recipe -> no leftover sharing between recipes, and the
    recipe uses the full package so leftover (waste) is 0. This isolates the
    variety behaviour from provisioning side effects.
    """
    prod = product(
        f"prod_{rid}", rid, [package(f"prod_{rid}", "100", "g", price)], category="misc"
    )
    rec = recipe(rid, {meal_type}, [ingredient(rid, "100", "g")], servings=2)
    return prod, rec


def _rich_pool():
    """Pool with more distinct recipes than slots for every meal type.

    breakfast=4 (>=2), lunch=6 (>=4), snack=3 (>=1), dinner=5 (>=3): up to 10
    distinct dishes are achievable for the 10 slots.
    """
    catalog = []
    candidates = []
    spec = [
        ("breakfast", 4, "2.00"),
        ("lunch", 6, "3.00"),
        ("snack", 3, "1.00"),
        ("dinner", 5, "2.50"),
    ]
    for meal_type, n, price in spec:
        for i in range(n):
            prod, rec = _solo(f"{meal_type[:2]}{i}", meal_type, price)
            catalog.append(prod)
            candidates.append(rec)
    return catalog, candidates


def test_variety_high_distinct_no_heavy_repetition():
    """A 10-meal plan over a rich pool yields many DISTINCT dishes.

    With one distinct recipe per slot achievable, the superlinear reuse penalty
    (w.repetition=12) makes reuse far costlier than any waste/time gap, so the
    optimizer should reach close to the 10-distinct ceiling. We assert >= 8 (a
    robust margin below the 10 ceiling, tolerating a couple of tie-driven reuses)
    and that no single recipe is used more than twice.
    """
    catalog, candidates = _rich_pool()
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=TEN_MEAL_REQS,
            catalog=catalog,
            candidates=candidates,
            budget_amount="200",
        )
    )
    assert isinstance(res, PlanResult)
    assert len(res.planned_meals) == 10
    counts = Counter(m.recipe_id for m in res.planned_meals)
    distinct = len(counts)
    assert distinct >= 8, f"expected varied plan, got {distinct} distinct: {counts}"
    assert max(counts.values()) <= 2, f"a dish repeated > 2x: {counts}"


def test_variety_holds_within_meal_type():
    """No lunch recipe is used 4x when 6 distinct lunches are available."""
    catalog, candidates = _rich_pool()
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=TEN_MEAL_REQS,
            catalog=catalog,
            candidates=candidates,
            budget_amount="200",
        )
    )
    assert isinstance(res, PlanResult)
    lunch_counts = Counter(
        m.recipe_id for m in res.planned_meals if m.meal_type == "lunch"
    )
    assert len(lunch_counts) == 4, f"lunches not all distinct: {lunch_counts}"


def test_budget_fit_not_trivially_minimized():
    """Default (waste) priority: stay <= budget WITHOUT collapsing to the cheapest dish.

    We add one ultra-cheap recipe per meal type. The old cost-minimizing objective
    would repeat those cheap dishes and land far below budget with ~3 distinct
    recipes. Under the envelope objective the plan stays within budget yet remains
    varied (>= 8 distinct) and spends materially more than the degenerate
    all-cheapest plan would.
    """
    catalog, candidates = _rich_pool()
    # One near-free recipe per meal type; if the optimizer minimized cost it would
    # fill every slot of that type with these.
    for meal_type in ("breakfast", "lunch", "snack", "dinner"):
        prod, rec = _solo(f"cheap_{meal_type}", meal_type, "0.10")
        catalog.append(prod)
        candidates.append(rec)

    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=TEN_MEAL_REQS,
            catalog=catalog,
            candidates=candidates,
            budget_amount="200",
        )
    )
    assert isinstance(res, PlanResult)
    # Within the envelope (budget_diff = budget.amount - cost_total).
    assert res.budget_diff >= 0, "plan must stay within budget"
    counts = Counter(m.recipe_id for m in res.planned_meals)
    assert len(counts) >= 8, f"collapsed to few dishes: {counts}"
    # Degenerate all-cheapest plan would cost ~10 * 0.10 = 1.00. A varied plan
    # spends materially more; assert it is not trivially minimized.
    assert res.cost_total.total > 1, f"plan trivially minimized: {res.cost_total.total}"


def test_price_priority_prefers_cheaper_plan():
    """priority='price' activates the cost term; the plan is cheaper than under 'waste'.

    Four lunch slots. Eight lunches, each consuming a full private package (waste 0,
    equal time), so under 'waste' priority every candidate ties and the search
    resolves by recipe_id -> it picks the lexicographically-smallest ids, which we
    make the EXPENSIVE ones. Under 'price' the cost term breaks the tie toward the
    cheap ids, producing a strictly cheaper plan.
    """
    catalog = []
    candidates = []
    for i in range(4):  # expensive, ids sort first ("a_...")
        prod, rec = _solo(f"a_exp{i}", "lunch", "5.00")
        catalog.append(prod)
        candidates.append(rec)
    for i in range(4):  # cheap, ids sort last ("z_...")
        prod, rec = _solo(f"z_chp{i}", "lunch", "1.00")
        catalog.append(prod)
        candidates.append(rec)

    def run(priority: str):
        return generate_plan(
            plan_input(
                members=[member("A")],
                requirements=[requirement("lunch", 4)],
                catalog=catalog,
                candidates=candidates,
                budget_amount="100",
                priority=priority,
            )
        )

    waste_res = run("waste")
    price_res = run("price")
    assert isinstance(waste_res, PlanResult)
    assert isinstance(price_res, PlanResult)
    assert price_res.cost_total.total < waste_res.cost_total.total, (
        f"price priority not cheaper: price={price_res.cost_total.total} "
        f"waste={waste_res.cost_total.total}"
    )
    # Both stay varied (4 distinct lunches) — price picks the 4 cheap ones.
    assert len({m.recipe_id for m in price_res.planned_meals}) == 4
    price_ids = {m.recipe_id for m in price_res.planned_meals}
    assert all(rid.startswith("z_chp") for rid in price_ids), price_ids


def test_strict_over_budget_still_infeasible():
    """The envelope cap is still hard under strict budgets: over budget -> infeasible."""
    from cestaplan_engine.contracts import InfeasibleResult

    prod, rec = _solo("lunchX", "lunch", "5.00")
    res = generate_plan(
        plan_input(
            members=[member("A")],
            requirements=[requirement("lunch", 1)],
            catalog=[prod],
            candidates=[rec],
            budget_amount="1.00",  # below the 5.00 cost -> no feasible plan
        )
    )
    assert isinstance(res, InfeasibleResult)


def test_price_priority_reproducible():
    """Determinism holds under the retuned objective for both priorities."""
    catalog, candidates = _rich_pool()

    def run(priority: str):
        return generate_plan(
            plan_input(
                members=[member("A")],
                requirements=TEN_MEAL_REQS,
                catalog=catalog,
                candidates=candidates,
                budget_amount="200",
                priority=priority,
                seed=7,
            )
        )

    assert run("waste").model_dump_json() == run("waste").model_dump_json()
    assert run("price").model_dump_json() == run("price").model_dump_json()


# --------------------------------------------------------------------------- #
# Budget-as-hard-constraint regression (a feasible-under-budget plan must NOT
# be reported infeasible just because the variety-optimal plan overshoots).
# --------------------------------------------------------------------------- #
# 9-meal scenario (2 breakfast / 4 lunch / 3 dinner). Each meal type has enough
# EXPENSIVE recipes (ids "a_*" @ 4.00) and CHEAP ones (ids "z_*" @ 1.00), each on
# its own private full-package product (waste 0, equal time). The variety-optimal
# search under "waste" resolves ties by recipe_id, so it picks the "a_*" expensive
# dishes -> total 36.00. The cheapest all-distinct plan is 9.00. Any budget in
# [9, 36) is therefore feasible but BELOW the variety-optimal cost.
NINE_MEAL_REQS = [
    requirement("breakfast", 2),
    requirement("lunch", 4),
    requirement("dinner", 3),
]
_VARIETY_OPTIMAL_COST = 36  # 2*4 + 4*4 + 3*4
_CHEAPEST_COST = 9  # 2*1 + 4*1 + 3*1


def _priced_pool():
    catalog = []
    candidates = []
    spec = [("breakfast", 2), ("lunch", 4), ("dinner", 3)]
    for meal_type, n in spec:
        for i in range(n):  # expensive, ids sort first
            prod, rec = _solo(f"a_{meal_type[:2]}{i}", meal_type, "4.00")
            catalog.append(prod)
            candidates.append(rec)
        for i in range(n):  # cheap, ids sort last
            prod, rec = _solo(f"z_{meal_type[:2]}{i}", meal_type, "1.00")
            catalog.append(prod)
            candidates.append(rec)
    return catalog, candidates


def _run_priced(budget_amount: str):
    catalog, candidates = _priced_pool()
    return generate_plan(
        plan_input(
            members=[member("A")],
            requirements=NINE_MEAL_REQS,
            catalog=catalog,
            candidates=candidates,
            budget_amount=budget_amount,
        )
    )


def test_tight_but_feasible_budget_returns_plan_not_infeasible():
    """REGRESSION: budget below the variety-optimal cost but above the cheapest
    plan must yield a PlanResult within budget — never InfeasibleResult.

    Before the fix, the waste-priority search built the ~36.00 variety-optimal
    plan, marked it over-budget and reported infeasible even though many plans fit
    under 15.00. The budget is a hard constraint: the optimizer must trade variety
    for cost only as much as needed to fit.
    """
    limit = 15
    assert _CHEAPEST_COST < limit < _VARIETY_OPTIMAL_COST  # a genuinely tight budget
    res = _run_priced(str(limit))
    assert isinstance(res, PlanResult), "feasible-under-budget plan wrongly infeasible"
    assert res.cost_total.total <= limit
    assert res.budget_diff >= 0
    assert len(res.planned_meals) == 9


def test_budget_hard_constraint_invariant():
    """Invariant: whenever a plan fitting the cap exists (cheapest_cost <= limit),
    generate_plan returns a PlanResult whose cost_total <= limit; only when even the
    cheapest plan exceeds the cap is InfeasibleResult returned, with a CONSISTENT
    min_budget_found > limit.
    """
    from cestaplan_engine.contracts import InfeasibleResult

    # Feasible band: limit >= cheapest (9) -> PlanResult within budget.
    for limit in (_CHEAPEST_COST, 10, 15, _VARIETY_OPTIMAL_COST, 60):
        res = _run_priced(str(limit))
        assert isinstance(res, PlanResult), f"budget {limit} should be feasible"
        assert res.cost_total.total <= limit, (
            f"budget {limit}: cost {res.cost_total.total} exceeds cap"
        )

    # Infeasible band: limit < cheapest (9) -> InfeasibleResult, message consistent.
    for limit in (5, 8):
        res = _run_priced(str(limit))
        assert isinstance(res, InfeasibleResult), f"budget {limit} should be infeasible"
        assert res.min_budget_found is not None
        assert res.min_budget_found > limit, (
            f"budget {limit}: min_budget_found {res.min_budget_found} must exceed cap"
        )


def test_comfortable_budget_still_varied_after_fix():
    """The hard-constraint fix must not regress the comfortable-budget quality:
    a generous budget still yields the full variety-optimal plan (9 distinct)."""
    res = _run_priced(str(_VARIETY_OPTIMAL_COST))
    assert isinstance(res, PlanResult)
    counts = Counter(m.recipe_id for m in res.planned_meals)
    assert len(counts) == 9, f"expected 9 distinct at a generous budget, got {counts}"
