"""C1: apply_promotion nunca produce coste negativo y normaliza el % (fracción o entero).

El bug: base*(1-pct) con pct=20 (porcentaje entero) daba -19·base. Fix: _discount_fraction
normaliza a fracción [0,1]. Tests herméticos con PromotionRule transitorio (sin BD).
"""

from __future__ import annotations

from decimal import Decimal

from cestaplan_api.models import PromotionRule
from cestaplan_api.services.basket_resolver import apply_promotion


def _rule(**kw: object) -> PromotionRule:
    base: dict[str, object] = {
        "type": None, "required_quantity": None, "charged_quantity": None,
        "percentage_discount": None, "fixed_discount": None,
    }
    base.update(kw)
    return PromotionRule(**base)


def test_percentage_integer_convention_is_not_negative() -> None:
    # pct=20 (entero) sobre 1 paquete de 1,80 € -> 20 % -> 1,44 € (antes: -34,20 € / 0 €).
    rule = _rule(type="percentage", percentage_discount=Decimal("20"))
    cost, applied = apply_promotion(rule, 1, Decimal("1.80"))
    assert cost == Decimal("1.44")
    assert applied is not None and applied.description == "-20%"


def test_percentage_fraction_convention_matches_integer() -> None:
    rule = _rule(type="percentage", percentage_discount=Decimal("0.20"))
    cost, _ = apply_promotion(rule, 1, Decimal("1.80"))
    assert cost == Decimal("1.44")


def test_percentage_hundred_is_free_not_negative() -> None:
    rule = _rule(type="percentage", percentage_discount=Decimal("100"))
    cost, _ = apply_promotion(rule, 2, Decimal("2.00"))
    assert cost == Decimal("0")


def test_percentage_respects_min_quantity() -> None:
    rule = _rule(type="percentage", percentage_discount=Decimal("50"), required_quantity=3)
    cost, applied = apply_promotion(rule, 2, Decimal("2.00"))  # 2 < 3 -> sin descuento
    assert cost == Decimal("4.00") and applied is None


def test_second_unit_integer_percentage() -> None:
    # 2 paquetes a 2,00 €, 2ª unidad -50 %: 1 completa (2,00) + 1 a mitad (1,00) = 3,00 €.
    rule = _rule(type="second_unit", percentage_discount=Decimal("50"), required_quantity=2)
    cost, applied = apply_promotion(rule, 2, Decimal("2.00"))
    assert cost == Decimal("3.00")
    assert applied is not None and applied.description == "2ª unidad -50%"


def test_nxm_unchanged() -> None:
    # 2x1: 2 paquetes, pagas 1 -> 2,00 €.
    rule = _rule(type="nxm", required_quantity=2, charged_quantity=1)
    cost, applied = apply_promotion(rule, 2, Decimal("2.00"))
    assert cost == Decimal("2.00") and applied is not None and applied.description == "2x1"


def test_fixed_floors_at_zero() -> None:
    rule = _rule(type="fixed", fixed_discount=Decimal("5.00"))
    cost, _ = apply_promotion(rule, 1, Decimal("2.00"))  # 2 - 5 -> 0, no negativo
    assert cost == Decimal("0")


def test_no_rule_returns_base() -> None:
    cost, applied = apply_promotion(None, 3, Decimal("1.50"))
    assert cost == Decimal("4.50") and applied is None
