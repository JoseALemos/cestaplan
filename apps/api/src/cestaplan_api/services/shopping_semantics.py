"""Shopping-list price/cost semantics (presentation-neutral, read-only).

The engine and persistence already store everything a grocery line needs; this module derives the
*correctly-labelled* magnitudes from those stored fields, so the serializer never conflates a
whole-package price with a per-gram reference price:

* ``package_price``         - buyable price of ONE package (€) = the real "€/envase";
* ``normalized_unit_price`` - a readable reference price (€/kg, €/l or €/unidad);
* ``purchased_cost``        - full-package outlay for the line (packages * package_price);
* ``consumed_cost``         - proportional value of the quantity actually used;
* ``leftover_value``        - purchased_cost minus consumed_cost;
* ``PriceSourceKind``       - demo / confirmed_external / estimated / unavailable.

Pure functions over primitives (no DB, no network); safe to unit-test directly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_CENT = Decimal("0.01")
# Unit families and the factor from the stored unit to its canonical base (g for mass, ml for vol).
_MASS = {"g": Decimal("1"), "kg": Decimal("1000")}
_VOLUME = {"ml": Decimal("1"), "cl": Decimal("10"), "l": Decimal("1000")}
_COUNT = {"unit": Decimal("1"), "ud": Decimal("1")}


class PriceSourceKind(StrEnum):
    """Where a line's price actually comes from (never conflate demo with a real observation)."""

    DEMO = "demo"
    CONFIRMED_EXTERNAL = "confirmed_external"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


def resolve_source_kind(source_type: str | None, price_status: str) -> PriceSourceKind:
    """Classify a line's price provenance from its source_type + price_status.

    Demo data is always demo (even when its status is 'known'); a missing price is unavailable; an
    estimated price/status is estimated; a real observed price is confirmed_external.
    """
    st = (source_type or "").lower()
    if price_status == "missing" or (not st and price_status not in ("known", "stale")):
        return PriceSourceKind.UNAVAILABLE
    if st == "demo":
        return PriceSourceKind.DEMO
    if st == "estimated" or price_status == "estimated":
        return PriceSourceKind.ESTIMATED
    if price_status in ("known", "stale"):
        return PriceSourceKind.CONFIRMED_EXTERNAL
    return PriceSourceKind.UNAVAILABLE


def package_price(total_cost: Decimal | None, packages: int | None) -> Decimal | None:
    """The buyable price of ONE package = line outlay / packages bought. None when unknown."""
    if total_cost is None or not packages or packages <= 0:
        return None
    return (total_cost / Decimal(packages)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _base(unit: str | None) -> tuple[str, Decimal] | None:
    u = (unit or "").lower()
    if u in _MASS:
        return "mass", _MASS[u]
    if u in _VOLUME:
        return "volume", _VOLUME[u]
    if u in _COUNT:
        return "count", _COUNT[u]
    return None


def normalized_unit_price(
    pkg_price: Decimal | None, pkg_quantity: Decimal | None, pkg_unit: str | None
) -> tuple[Decimal, str] | None:
    """Readable reference price + unit: €/kg (mass), €/l (volume) or €/unidad (count).

    Computed from the package price and its net content — never a per-gram value rounded to cents.
    """
    if pkg_price is None or pkg_quantity is None or pkg_quantity <= 0:
        return None
    base = _base(pkg_unit)
    if base is None:
        return None
    family, factor = base
    base_qty = pkg_quantity * factor  # in g / ml / unit
    if base_qty <= 0:
        return None
    if family == "count":
        return (pkg_price / base_qty).quantize(_CENT, rounding=ROUND_HALF_UP), "unidad"
    # mass -> €/kg, volume -> €/l  (price per 1000 base units)
    per_base = pkg_price / base_qty
    return (per_base * Decimal("1000")).quantize(_CENT, rounding=ROUND_HALF_UP), (
        "kg" if family == "mass" else "l"
    )


def line_cost_breakdown(
    total_cost: Decimal | None,
    purchased_quantity: Decimal | None,
    used_quantity: Decimal | None,
) -> dict[str, Decimal | None]:
    """purchased/consumed/leftover money for a line (proportional to the amount actually used)."""
    if total_cost is None:
        return {"purchased_cost": None, "consumed_cost": None, "leftover_value": None}
    purchased = total_cost.quantize(_CENT, rounding=ROUND_HALF_UP)
    if purchased_quantity and purchased_quantity > 0 and used_quantity is not None:
        ratio = used_quantity / purchased_quantity
        consumed = (total_cost * ratio).quantize(_CENT, rounding=ROUND_HALF_UP)
    else:
        consumed = purchased
    leftover = (purchased - consumed).quantize(_CENT, rounding=ROUND_HALF_UP)
    return {"purchased_cost": purchased, "consumed_cost": consumed, "leftover_value": leftover}


__all__ = [
    "PriceSourceKind",
    "line_cost_breakdown",
    "normalized_unit_price",
    "package_price",
    "resolve_source_kind",
]
