"""Mercadona coverage onboarder: the selector picks the plain staple and rejects prepared junk."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from cestaplan_api.tools.onboard_mercadona_coverage import _choose


def _v(vid: int, name: str, qty: str | None = None, unit: str | None = None,
       uprice: str | None = None, pid: int | None = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=vid,
        display_name=name,
        net_content_quantity=Decimal(qty) if qty else None,
        net_content_unit=unit,
        unit_price=Decimal(uprice) if uprice else None,
        product_id=pid,
    )


def test_platano_picks_fresh_fruit_not_smoothie_or_dessert() -> None:
    variants = [
        _v(1, "Smoothie fresa y plátano Hacendado", "0.25", "l", pid=10),
        _v(2, "Plátano de Canarias IGP", "0.17", "kg", pid=20),
        _v(3, "Danonino sabor fresa y plátano Danone", "0.30", "kg", pid=30),
    ]
    chosen = _choose("plátano", variants, {10: Decimal("1"), 20: Decimal("1.80"), 30: Decimal("2")})
    assert chosen is not None
    assert chosen.variant.id == 2


def test_naranja_picks_fruit_not_soda_or_juice() -> None:
    variants = [
        _v(1, "Refresco Fanta naranja", "2", "l", pid=10),
        _v(2, "Naranjas", "5", "kg", pid=20),
        _v(3, "Zumo de naranja exprimido Hacendado", "1", "l", pid=30),
    ]
    chosen = _choose("naranja", variants, {10: Decimal("1"), 20: Decimal("6"), 30: Decimal("2")})
    assert chosen is not None
    assert chosen.variant.id == 2


def test_all_prepared_yields_no_mapping() -> None:
    variants = [
        _v(1, "Batido de fresa Hacendado", "1", "l", pid=10),
        _v(2, "Mermelada de fresa Hacendado", "0.4", "kg", pid=20),
    ]
    assert _choose("fresa", variants, {10: Decimal("1"), 20: Decimal("3")}) is None


def test_unmapped_variant_is_never_eligible() -> None:
    # A variant not linked to a canonical Product (product_id None) can't be mapped.
    variants = [_v(1, "Pan de molde blanco Hacendado", "0.46", "kg", pid=None)]
    assert _choose("pan de molde", variants, {}) is None


def test_plain_packaged_staple_is_chosen() -> None:
    variants = [
        _v(1, "Pan de molde blanco Hacendado", "0.46", "kg", pid=40),
        _v(2, "Pan de molde 100% integral tostado sabor Hacendado", "0.5", "kg", pid=41),
    ]
    chosen = _choose("pan de molde", variants, {40: Decimal("1.74"), 41: Decimal("2.5")})
    assert chosen is not None
    assert chosen.variant.id == 1  # the plainer staple (variant 2 carries "sabor" -> junk)
