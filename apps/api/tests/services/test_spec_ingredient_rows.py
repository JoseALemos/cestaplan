"""Integrity guard: every ``_SPECS`` key must be a real ``ingredient.canonical_name``.

Costing binds a recipe to a spec through the ingredient identity (the canonical name / id), so
a spec whose key does not correspond to any ingredient row is dead weight that can never match.

The oracle is ``tests/fixtures/prod_ingredient_canonical_names.json`` — a READ-ONLY snapshot of
``SELECT canonical_name FROM ingredient`` taken from production (the local demo seed only carries a
subset, so it cannot serve as the reference). If a spec key is ever added without a backing
ingredient row, this test fails and the key must be dropped or the row created by a migration
(out of scope here).
"""

from __future__ import annotations

import json
from pathlib import Path

from cestaplan_api.services.ingredient_dictionary import _SPECS

_SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "prod_ingredient_canonical_names.json"
)


def _prod_names() -> set[str]:
    return set(json.loads(_SNAPSHOT.read_text(encoding="utf-8")))


def test_every_spec_key_is_a_real_ingredient_row() -> None:
    prod_names = _prod_names()
    assert prod_names, "prod ingredient-name snapshot is empty"
    missing = sorted(key for key in _SPECS if key not in prod_names)
    assert missing == [], f"_SPECS keys with no ingredient row: {missing}"


def test_spec_key_matches_its_own_key_field() -> None:
    # The IngredientSpec.key field must equal its dict key (used as the canonical identity).
    mismatched = {k: spec.key for k, spec in _SPECS.items() if spec.key != k}
    assert mismatched == {}, mismatched
