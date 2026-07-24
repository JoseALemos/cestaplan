"""Alcampo mapper evidence-based sale basis (audit §2) — offline, no network.

The mapper must only emit a fixed net content when the size is a clean, unambiguous package; a
size range or an 'al peso' item must NOT masquerade as a fixed package.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cestaplan_api.ingestion.providers.onboarding import classify_costing_mode
from cestaplan_api.ingestion.providers.parsebot.chains import ParseBotAlcampoMapper

_NOW = datetime(2026, 7, 24, tzinfo=UTC)
_MAPPER = ParseBotAlcampoMapper()


def _record(name: str, size: str, *, amount: str = "2.49", up_kg: str | None = "3.56") -> dict:
    rec: dict = {
        "productId": "uuid",
        "retailerProductId": "1",
        "type": "REGULAR",
        "name": name,
        "brand": "MARCA",
        "categoryPath": ["Frescos", "Frutas"],
        "price": {"amount": amount, "currency": "EUR"},
        "packSizeDescription": size,
        "available": True,
    }
    if up_kg is not None:
        rec["unitPrice"] = {
            "price": {"amount": up_kg, "currency": "EUR"},
            "unitName": "PER_1KG",
        }
    return rec


def test_fixed_tray_is_a_fixed_package() -> None:
    p = _MAPPER.map_product(_record("Plátano de Canarias bandeja 700 g", "700g"), retrieved_at=_NOW)
    assert p.variable_weight is False
    assert p.net_content_quantity is not None
    assert classify_costing_mode(p).value == "fixed_package"


def test_size_range_is_not_a_fixed_package() -> None:
    p = _MAPPER.map_product(_record("Plátano Canario IGP bolsa", "750g - 1250g"), retrieved_at=_NOW)
    assert p.net_content_quantity is None  # a range is not a clean net content
    assert classify_costing_mode(p).value == "unresolved"


def test_sold_al_peso_is_not_a_fixed_package() -> None:
    p = _MAPPER.map_product(_record("Plátano macho al peso", "375g - 625g"), retrieved_at=_NOW)
    assert p.net_content_quantity is None
    assert classify_costing_mode(p).value != "fixed_package"


def test_unit_price_is_kept_as_reference_on_a_fixed_package() -> None:
    p = _MAPPER.map_product(
        _record("Plátano bandeja 700 g", "700g", up_kg="3.56"), retrieved_at=_NOW
    )
    # The €/kg is retained but the buyable price is the package price.
    assert p.regular_price is not None
    assert str(p.regular_price) == "2.49"
    assert p.unit_price is not None and p.unit_price_unit == "kg"
