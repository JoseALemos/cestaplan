"""The shared price-scope zone-safety invariant (pure, no DB)."""

from __future__ import annotations

from cestaplan_api.services.price_scope import scope_satisfies


def test_national_price_satisfies_any_plan() -> None:
    assert scope_satisfies("national", "national") is True
    assert scope_satisfies("national", "postal_code") is True
    assert scope_satisfies("national", "exact_store") is True


def test_zonified_price_never_satisfies_a_national_plan() -> None:
    # The whole point: a no-zone (national) plan is NEVER served a zoned price.
    assert scope_satisfies("postal_code", "national") is False
    assert scope_satisfies("exact_store", "national") is False
    assert scope_satisfies("delivery_zone", "national") is False


def test_broader_or_equal_satisfies_a_zoned_plan() -> None:
    # A store plan accepts its own postal area price and any broader scope, never a narrower one.
    assert scope_satisfies("postal_code", "exact_store") is True  # postal contains the store
    assert scope_satisfies("exact_store", "exact_store") is True
    assert scope_satisfies("exact_store", "postal_code") is False  # a single store isn't the postal


def test_unknown_is_only_compatible_with_unknown() -> None:
    assert scope_satisfies("unknown", "unknown") is True
    assert scope_satisfies("unknown", "national") is False
    assert scope_satisfies("national", "unknown") is False
