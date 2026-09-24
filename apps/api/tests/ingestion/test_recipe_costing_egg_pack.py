"""C3: el carril de costeo-SOMBRA costea los huevos por PAQUETE (docena), no por cartón suelto.

Paridad con el carril de planificación (current_price._package_dims). Tests herméticos (sin BD):
ProductVariant transitorio + _Candidate + llamadas directas a _count_pack_units/_cost_candidate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.ingestion.providers.contracts import ProductCostingMode
from cestaplan_api.models import ProductVariant
from cestaplan_api.services.recipe_costing import _Candidate, _cost_candidate, _count_pack_units


def _variant(**kw: object) -> ProductVariant:
    base: dict[str, object] = {
        "display_name": None, "sell_unit": "package",
        "package_quantity": None, "package_unit": None,
        "net_content_quantity": None, "net_content_unit": None,
        "unit_price": None, "unit_price_unit": None, "variable_weight": False,
    }
    base.update(kw)
    return ProductVariant(**base)


def _cand(v: ProductVariant, price: str, mode: ProductCostingMode) -> _Candidate:
    return _Candidate(
        variant=v, price=Decimal(price), price_scope="national", fresh=True, mode=mode
    )


def test_count_pack_units_egg_name_is_dozen() -> None:
    assert _count_pack_units(_variant(display_name="Huevos grandes L")) == Decimal("12")


def test_count_pack_units_explicit_package_quantity() -> None:
    v = _variant(display_name="Tomate", package_quantity=Decimal("6"), package_unit="unit")
    assert _count_pack_units(v) == Decimal("6")


def test_count_pack_units_non_egg_without_pack_is_none() -> None:
    assert _count_pack_units(_variant(display_name="Lechuga iceberg")) is None


def test_cost_candidate_eggs_buys_whole_dozen_not_per_carton() -> None:
    # 6 huevos, cartón de docena a 1,80 € -> 1 paquete, 12 unidades compradas, 1,80 €.
    v = _variant(display_name="Huevos grandes L")
    cand = _cand(v, "1.80", ProductCostingMode.DISCRETE_UNIT)
    got = _cost_candidate(Decimal("6"), "count", cand)
    assert got == (Decimal("1"), Decimal("12"), Decimal("1.80"))


def test_cost_candidate_eggs_two_dozen_when_needed() -> None:
    v = _variant(display_name="Huevos M")
    cand = _cand(v, "1.80", ProductCostingMode.DISCRETE_UNIT)
    # 13 huevos -> 2 docenas (24 uds), 3,60 €.
    assert _cost_candidate(Decimal("13"), "count", cand) == (
        Decimal("2"), Decimal("24"), Decimal("3.60")
    )


def test_cost_candidate_single_unit_item_unchanged() -> None:
    # Un artículo contado suelto (no huevo, sin pack) sigue siendo 1 pieza por unidad.
    v = _variant(display_name="Lechuga iceberg")
    cand = _cand(v, "0.90", ProductCostingMode.DISCRETE_UNIT)
    assert _cost_candidate(Decimal("3"), "count", cand) == (
        Decimal("3"), Decimal("3"), Decimal("2.70")
    )


def test_cost_candidate_explicit_pack_quantity() -> None:
    v = _variant(display_name="Yogur natural", package_quantity=Decimal("6"), package_unit="unit")
    cand = _cand(v, "1.50", ProductCostingMode.DISCRETE_UNIT)
    # 7 uds -> 2 packs de 6 (12 uds), 3,00 €.
    assert _cost_candidate(Decimal("7"), "count", cand) == (
        Decimal("2"), Decimal("12"), Decimal("3.00")
    )


@pytest.mark.parametrize("dim", ["mass", "volume"])
def test_cost_candidate_ignores_egg_rule_for_non_count(dim: str) -> None:
    # La regla de pack de conteo solo aplica a required_dim=='count'; masa/volumen no la tocan.
    v = _variant(display_name="Huevos grandes L")
    cand = _cand(v, "1.80", ProductCostingMode.DISCRETE_UNIT)
    assert _cost_candidate(Decimal("100"), dim, cand) is None
