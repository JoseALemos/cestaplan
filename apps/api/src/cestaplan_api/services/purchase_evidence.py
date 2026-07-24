"""Evidence-based purchase-mode resolution for a single product (audit §2/§6).

Given the confirmed fields of a product, decide HOW it can be bought and whether it is costing
eligible — never inventing a fixed package. The rules encode the audit:

* a clean fixed net content + a package price  -> ``fixed_package`` (buy whole packs);
* a genuine loose sale by weight/volume (variable_weight + a €/kg or €/l unit price) ->
  ``variable_weight`` / ``variable_volume`` (buy the required amount, respecting min/increment);
* a discrete count -> ``discrete_unit``;
* an approximate/reference weight WITHOUT a buyable net content or loose-sale rules ->
  ``unresolved`` (``costing_eligible = false``): the fraction cannot be honestly costed.

The result carries the evidence and, when not eligible, the precise blocker so a validation report
can explain exactly what is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from cestaplan_api.ingestion.providers.contracts import ProductCostingMode

_MASS = ("g", "kg")
_VOLUME = ("ml", "l")


@dataclass(slots=True)
class PurchaseEvidence:
    costing_mode: str
    costing_eligible: bool
    sell_basis: str  # package | weight | volume | unit | unknown
    net_content_quantity: Decimal | None = None
    net_content_unit: str | None = None
    package_price: Decimal | None = None
    price_is_package_price: bool = False
    unit_price: Decimal | None = None
    unit_price_unit: str | None = None
    approximate_weight: bool = False
    evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    blocker: str | None = None  # a §6 costing blocker when not eligible

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for k, v in asdict(self).items():
            out[k] = str(v) if isinstance(v, Decimal) else v
        return out


def resolve_purchase_evidence(
    *,
    name: str | None,
    required_unit: str | None,
    net_content_quantity: Decimal | None,
    net_content_unit: str | None,
    variable_weight: bool,
    sell_unit: str | None,
    regular_price: Decimal | None,
    unit_price: Decimal | None,
    unit_price_unit: str | None,
    has_price: bool,
) -> PurchaseEvidence:
    """Decide the definitive purchase mode + eligibility from confirmed fields only (§2)."""
    ncu = (net_content_unit or "").lower()
    upu = (unit_price_unit or "").lower()
    ev = PurchaseEvidence(
        costing_mode=ProductCostingMode.UNRESOLVED.value,
        costing_eligible=False,
        sell_basis="unknown",
        net_content_quantity=net_content_quantity,
        net_content_unit=net_content_unit,
        unit_price=unit_price,
        unit_price_unit=unit_price_unit,
    )
    if not has_price or regular_price is None:
        ev.missing_evidence.append("no usable price")
        ev.blocker = "incomplete_package_data"
        return ev
    if regular_price <= 0:
        ev.missing_evidence.append("non-positive price")
        ev.blocker = "incomplete_package_data"
        return ev

    # 1. genuine loose sale by weight/volume: variable + a real per-measure unit price.
    if variable_weight and unit_price is not None and unit_price > 0:
        if (sell_unit or "").lower() == "weight" and upu in _MASS:
            ev.costing_mode = ProductCostingMode.VARIABLE_WEIGHT.value
            ev.sell_basis = "weight"
            ev.costing_eligible = True
            ev.unit_price_unit = upu
            ev.evidence.append(f"sold by weight; unit price {unit_price} €/{upu}")
            return ev
        if (sell_unit or "").lower() == "volume" and upu in _VOLUME:
            ev.costing_mode = ProductCostingMode.VARIABLE_VOLUME.value
            ev.sell_basis = "volume"
            ev.costing_eligible = True
            ev.unit_price_unit = upu
            ev.evidence.append(f"sold by volume; unit price {unit_price} €/{upu}")
            return ev

    # 2. a clean fixed net content + a package price -> fixed package.
    if net_content_quantity is not None and ncu in _MASS + _VOLUME:
        if required_unit is not None and _dim(required_unit) != _dim(ncu):
            ev.blocker = "unresolved_purchase_unit"
            ev.missing_evidence.append(
                f"recipe unit '{required_unit}' incompatible with pack unit '{ncu}'"
            )
            return ev
        ev.costing_mode = ProductCostingMode.FIXED_PACKAGE.value
        ev.sell_basis = "package"
        ev.costing_eligible = True
        ev.package_price = regular_price
        ev.price_is_package_price = True
        ev.evidence.append(
            f"fixed net content {net_content_quantity}{ncu}; package price {regular_price}"
        )
        if unit_price is not None:
            ev.evidence.append(f"unit price {unit_price} €/{upu or '?'} is informational")
        return ev

    # 3. a known discrete count.
    if net_content_quantity is not None and ncu in ("unit", "ud"):
        ev.costing_mode = ProductCostingMode.DISCRETE_UNIT.value
        ev.sell_basis = "unit"
        ev.costing_eligible = True
        ev.evidence.append(f"{net_content_quantity} discrete units")
        return ev
    if (sell_unit or "").lower() == "unit":
        ev.costing_mode = ProductCostingMode.DISCRETE_UNIT.value
        ev.sell_basis = "unit"
        ev.costing_eligible = True
        ev.evidence.append("sold per unit")
        return ev

    # 4. no clean net content: an informational €/kg without a buyable pack is an APPROXIMATE
    #    weight without rules; otherwise the package data is simply incomplete.
    ev.approximate_weight = unit_price is not None and upu in _MASS + _VOLUME
    if ev.approximate_weight:
        ev.blocker = "approximate_weight_without_rules"
        ev.missing_evidence.append(
            "reference unit price present but no buyable net content / loose-sale rules"
        )
        ev.evidence.append(f"reference {unit_price} €/{upu} is informational only")
    else:
        ev.blocker = "incomplete_package_data"
        ev.missing_evidence.append("no net content, no unit price, no discrete count")
    return ev


def _dim(unit: str) -> str:
    u = unit.lower()
    if u in _MASS:
        return "mass"
    if u in _VOLUME:
        return "volume"
    return u


__all__ = ["PurchaseEvidence", "resolve_purchase_evidence"]
