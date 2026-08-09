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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from cestaplan_api.ingestion.current_price import CurrentPrice, CurrentPriceService

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


def gated_current_price(
    prices: CurrentPriceService,
    db: Session,
    product_variant_id: int,
    *,
    store_id: int | None,
    required_scope: str,
    as_of: datetime,
    staging: bool = False,
) -> CurrentPrice | None:
    """Current price for a variant that SATISFIES ``required_scope``, with a national fallback.

    The store-scoped current price is used only when its scope satisfies the requirement. Otherwise
    — the value-matched ``current()`` can return a NEWER zonified observation that would either be
    served to a no-zone (national) plan or shadow a valid national price — this falls back to the
    variant's latest ``national`` observation (which satisfies any plan). Returns ``None`` when
    neither is usable, so a plan is NEVER costed with a price from the wrong area.

    ``prices`` (a :class:`CurrentPriceService`) is injected so this module keeps no runtime
    dependency on the ingestion layer; it is the single shared read used by every scope-aware
    costing path (recipe costing, basket resolution, costing-readiness validation).
    """
    price = prices.current(db, product_variant_id, store_id=store_id, as_of=as_of, staging=staging)
    if price is not None and scope_satisfies(price.price_scope, required_scope):
        return price
    national = prices.current(
        db, product_variant_id, store_id=store_id, scope="national", as_of=as_of, staging=staging
    )
    if national is not None and scope_satisfies(national.price_scope, required_scope):
        return national
    return None


__all__ = ["SCOPE_RANK", "gated_current_price", "scope_satisfies"]
