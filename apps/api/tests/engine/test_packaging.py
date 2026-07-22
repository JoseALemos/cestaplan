"""Whole-package selection (OPTIMIZATION.md §3). Money is exact Decimal."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cestaplan_engine.packaging import (
    PackageOptimizer,
    compute_packages,
    decimal_ceil_div,
)

from .builders import package


def test_canonical_example_600g_chicken():
    # 600 g needed, 500 g packs at 4.20, empty pantry.
    r = compute_packages(Decimal("600"), Decimal("0"), Decimal("500"), Decimal("4.20"))
    assert r.packages == 2
    assert r.purchased == Decimal("1000")
    assert r.used == Decimal("600")
    assert r.leftover == Decimal("400")
    assert r.total_cost == Decimal("8.40")
    assert isinstance(r.total_cost, Decimal)


def test_forbidden_fractional_cost_is_not_used():
    r = compute_packages(Decimal("600"), Decimal("0"), Decimal("500"), Decimal("4.20"))
    # The wrong answer would be 600/500 * 4.20 = 5.04. We buy whole packs.
    assert r.total_cost != Decimal("5.04")
    assert r.total_cost == Decimal("8.40")


def test_exact_multiple_no_leftover():
    r = compute_packages(Decimal("1000"), Decimal("0"), Decimal("500"), Decimal("4.20"))
    assert r.packages == 2
    assert r.purchased == Decimal("1000")
    assert r.leftover == Decimal("0")
    assert r.total_cost == Decimal("8.40")


def test_pantry_covers_all_zero_packages():
    r = compute_packages(Decimal("600"), Decimal("600"), Decimal("500"), Decimal("4.20"))
    assert r.pending == Decimal("0")
    assert r.packages == 0
    assert r.purchased == Decimal("0")
    assert r.total_cost == Decimal("0")


def test_pantry_reduces_pending():
    r = compute_packages(Decimal("600"), Decimal("200"), Decimal("500"), Decimal("4.20"))
    assert r.pending == Decimal("400")
    assert r.packages == 1
    assert r.purchased == Decimal("500")
    assert r.used == Decimal("400")
    assert r.leftover == Decimal("100")
    assert r.total_cost == Decimal("4.20")


def test_pending_zero_when_pantry_exceeds_need():
    r = compute_packages(Decimal("300"), Decimal("500"), Decimal("500"), Decimal("4.20"))
    assert r.pending == Decimal("0")
    assert r.packages == 0
    assert r.total_cost == Decimal("0")


def test_decimal_ceil_div():
    assert decimal_ceil_div(Decimal("600"), Decimal("500")) == 2
    assert decimal_ceil_div(Decimal("1000"), Decimal("500")) == 2
    assert decimal_ceil_div(Decimal("0"), Decimal("500")) == 0
    assert decimal_ceil_div(Decimal("1"), Decimal("500")) == 1


def test_optimizer_prefers_lower_waste_and_cost_format():
    opt = PackageOptimizer()
    small = package("chicken", "500", "g", "4.20")
    big = package("chicken", "1000", "g", "9.00")
    # need 600 g: small -> 2 packs=1000g cost 8.40 leftover 400
    #             big   -> 1 pack =1000g cost 9.00 leftover 400
    choice = opt.choose(Decimal("600"), [small, big])
    assert choice is not None
    assert choice.option.package_quantity == Decimal("500")
    assert choice.result.total_cost == Decimal("8.40")


def test_optimizer_prefers_known_price_over_estimated():
    opt = PackageOptimizer()
    estimated = package("x", "500", "g", "3.00", has_price=False)
    known = package("x", "500", "g", "4.00", has_price=True)
    choice = opt.choose(Decimal("500"), [estimated, known])
    assert choice is not None
    assert choice.price_known is True
    assert choice.option.amount == Decimal("4.00")


def test_optimizer_flags_expired_price():
    opt = PackageOptimizer()
    stale = package(
        "x", "500", "g", "4.00", expires_at=date(2026, 7, 1)
    )
    choice = opt.choose(Decimal("500"), [stale], as_of=date(2026, 7, 21))
    assert choice is not None
    assert choice.expired is True
    assert choice.price_known is False
