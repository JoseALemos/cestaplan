"""Single source of truth for the price-scope compatibility invariant (zone safety).

A price applies to a geographic/administrative area. A candidate price may be used to cost a plan
only when the candidate's area CONTAINS the plan's required area — i.e. the candidate is at least as
BROAD as the requirement. Concretely:

- a ``national`` price satisfies ANY plan (it applies everywhere);
- a zonified price (``exact_store``/``postal_code``/``province``/...) NEVER satisfies a broader or
  ``national`` requirement, so a no-zone plan is never served a zoned price and no plan is ever
  costed with a price from the wrong, narrower area;
- ``unknown`` is compatible only with an ``unknown`` requirement.

Every read path (recipe costing, catalog coverage, price fallback) imports this one rule so the
"a zone-A price is never served to a zone-B or no-zone plan" invariant can never silently diverge.
"""

from __future__ import annotations

# Lower rank = more specific / narrower area; higher rank = broader area.
SCOPE_RANK: dict[str, int] = {
    "exact_store": 1,
    "delivery_zone": 2,
    "postal_code": 3,
    "municipality": 4,
    "province": 5,
    "region": 6,
    "national": 7,
    "unknown": 8,
}


def scope_satisfies(candidate_scope: str, required_scope: str) -> bool:
    """True iff a ``candidate_scope`` price may cost a ``required_scope`` plan (see module doc)."""
    if candidate_scope == "unknown":
        return required_scope == "unknown"
    return SCOPE_RANK.get(candidate_scope, 0) >= SCOPE_RANK.get(required_scope, 99)


__all__ = ["SCOPE_RANK", "scope_satisfies"]
