"""Comparison input fingerprint (audit §1) — pure, no DB, no network."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cestaplan_api.services.recipe_shadow import comparison_input_fingerprint


@dataclass
class _RI:
    canonical_name: str
    quantity: Decimal
    unit: str
    optional: bool = False


@dataclass
class _Recipe:
    id: int
    servings: int
    ingredients: list[_RI]
    updated_at: object = None


def _recipe(**over) -> _Recipe:
    base: dict[str, object] = {
        "id": 1,
        "servings": 2,
        "ingredients": [
            _RI("avena_copos", Decimal("80"), "g"),
            _RI("leche_entera", Decimal("400"), "ml"),
            _RI("platano", Decimal("160"), "g"),
            _RI("miel", Decimal("15"), "g", optional=True),
        ],
    }
    base.update(over)
    return _Recipe(**base)  # type: ignore[arg-type]


def _fp(recipe, **over) -> str:
    kw: dict[str, object] = {
        "servings": recipe.servings,
        "included_optionals": [],
        "pantry_policy": "empty_pantry",
        "leftover_policy": "not_amortized_isolated",
    }
    kw.update(over)
    return comparison_input_fingerprint(recipe, **kw)  # type: ignore[arg-type]


def test_same_inputs_same_fingerprint() -> None:
    assert _fp(_recipe()) == _fp(_recipe())


def test_different_servings_changes_fingerprint() -> None:
    assert _fp(_recipe(), servings=2) != _fp(_recipe(), servings=4)


def test_different_quantity_changes_fingerprint() -> None:
    other = _recipe(
        ingredients=[
            _RI("avena_copos", Decimal("100"), "g"),  # 80 -> 100
            _RI("leche_entera", Decimal("400"), "ml"),
            _RI("platano", Decimal("160"), "g"),
        ]
    )
    assert _fp(_recipe()) != _fp(other)


def test_including_an_optional_changes_fingerprint() -> None:
    # A side that counts 'miel' has a different fingerprint from one that omits it.
    assert _fp(_recipe(), included_optionals=[]) != _fp(_recipe(), included_optionals=["miel"])


def test_pantry_policy_changes_fingerprint() -> None:
    assert _fp(_recipe(), pantry_policy="empty_pantry") != _fp(
        _recipe(), pantry_policy="plan_shared_inventory"
    )
