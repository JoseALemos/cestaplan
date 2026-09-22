"""Envase de huevos recuperado del nombre (fix del bug de los 19,80 €).

Los productos-huevo llegan sin envase estructurado; como el motor compra paquetes enteros, una
docena sin pack se costeaba como 12 cartones. El fix recupera el pack del nombre (docena por
defecto) en los dos puntos que producían el ``1/'unit'``: la proyección
(``CurrentPriceService._package_dims``) y la ingesta genérica (``orchestration._describe``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.ingestion.current_price import CurrentPriceService
from cestaplan_api.ingestion.normalization import egg_pack_size
from cestaplan_api.ingestion.orchestration import _describe
from cestaplan_api.models import ProductVariant


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Huevos grandes L", Decimal("12")),          # sin recuento -> docena por defecto
        ("Huevos medianos M", Decimal("12")),
        ("Huevos super grandes XL", Decimal("12")),
        ("Huevos M", Decimal("12")),
        ("Docena de huevos camperos", Decimal("12")),
        ("Media docena de huevos", Decimal("6")),
        ("Huevos camperos 6 ud", Decimal("6")),
        ("Huevos 10 unidades", Decimal("10")),
        ("Huevos frescos 12 huevos", Decimal("12")),
    ],
)
def test_egg_pack_size_recognises_eggs(name: str, expected: Decimal) -> None:
    assert egg_pack_size(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "Pasta al huevo 500 g",       # contiene "huevo" pero NO es un producto de huevos
        "Licor de huevo",
        "Leche entera 6 unidades",    # multipack de otro producto: no se asume nada
        "Tomate frito",
        "Mayonesa con huevo 450 ml",
    ],
)
def test_egg_pack_size_ignores_non_eggs(name: str | None) -> None:
    assert egg_pack_size(name) is None


def _variant(**kwargs: object) -> ProductVariant:
    base: dict[str, object] = {
        "display_name": None,
        "package_quantity": None,
        "package_unit": None,
        "net_content_quantity": None,
        "net_content_unit": None,
    }
    base.update(kwargs)
    return ProductVariant(**base)


def test_package_dims_eggs_without_packaging_defaults_to_dozen() -> None:
    variant = _variant(display_name="Huevos grandes L")
    assert CurrentPriceService._package_dims(variant) == (Decimal("12"), "unit")


def test_package_dims_non_egg_without_packaging_stays_single_unit() -> None:
    variant = _variant(display_name="Lechuga iceberg")
    assert CurrentPriceService._package_dims(variant) == (Decimal("1"), "unit")


def test_package_dims_prefers_explicit_variant_packaging_over_egg_rule() -> None:
    # Un envase explícito manda: la regla de huevos solo cubre la variante SIN envase.
    variant = _variant(
        display_name="Huevos grandes L", package_quantity=Decimal("6"), package_unit="unit"
    )
    assert CurrentPriceService._package_dims(variant) == (Decimal("6"), "unit")


def test_package_dims_prefers_net_content_over_egg_rule() -> None:
    variant = _variant(
        display_name="Huevos líquidos", net_content_quantity=Decimal("500"), net_content_unit="ml"
    )
    assert CurrentPriceService._package_dims(variant) == (Decimal("500"), "ml")


def test_describe_eggs_without_package_block_gets_dozen() -> None:
    name, brand, qty, unit, count = _describe("egg-1", {"name": "Huevos grandes L"})
    assert (qty, unit, count) == (Decimal("12"), "unit", 1)
    assert name == "Huevos grandes L" and brand is None


def test_describe_honours_explicit_package_block() -> None:
    raw = {"name": "Huevos M", "package": {"quantity": "6", "unit": "unit", "count": 1}}
    _, _, qty, unit, _ = _describe("egg-2", raw)
    assert (qty, unit) == (Decimal("6"), "unit")


def test_describe_non_egg_without_package_stays_unset() -> None:
    _, _, qty, unit, _ = _describe("veg-1", {"name": "Tomate pera"})
    assert qty is None and unit is None
