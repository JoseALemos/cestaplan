"""C5: un precio CADUCADO cuenta como estimado (no 'known') en la cesta.

El motor degrada caducado→estimado; la cesta debe hacer lo mismo para no reportar como coste
'conocido' un precio expirado. Test unitario de _is_degraded y de la propiedad is_estimated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.ingestion.current_price import FreshnessStatus
from cestaplan_api.services.basket_resolver import ResolvedLine, _is_degraded


@pytest.mark.parametrize(
    ("price_type", "status", "expected"),
    [
        ("regular", FreshnessStatus.FRESH, False),
        ("regular", FreshnessStatus.STALE, False),      # stale sigue siendo conocido (aún usable)
        ("regular", FreshnessStatus.EXPIRED, True),      # caducado -> estimado
        ("promotional", FreshnessStatus.EXPIRED, True),
        ("estimated", FreshnessStatus.FRESH, True),       # ya estimado por tipo
    ],
)
def test_is_degraded(price_type: str, status: FreshnessStatus, expected: bool) -> None:
    assert _is_degraded(price_type, status) is expected


def _line(price_type: str, status: FreshnessStatus) -> ResolvedLine:
    return ResolvedLine(
        item=None, variant_id=None, product_id=None, display_name="X",
        required_quantity=Decimal("1"), required_unit="unit",
        package_quantity=Decimal("1"), package_unit="unit",
        packages=1, purchased_quantity=Decimal("1"), used_quantity=Decimal("1"),
        leftover=Decimal("0"), unit_price=Decimal("1"), list_cost=Decimal("1"),
        line_cost=Decimal("1"), currency="EUR", promotion=None,
        price_type=price_type, price_scope="national", source_id=None,
        observed_at=None, age_seconds=Decimal("0"), freshness=status,
        confidence=Decimal("1"), available=True,
    )


def test_resolved_line_expired_is_estimated() -> None:
    assert _line("regular", FreshnessStatus.EXPIRED).is_estimated is True
    assert _line("regular", FreshnessStatus.FRESH).is_estimated is False
