"""Unit tests for the pure ingredient-consolidation planner (no DB, no network)."""

from __future__ import annotations

from cestaplan_api.services.ingredient_consolidation import (
    build_consolidation_plan,
)


def _merge_map(plan) -> dict[int, int]:
    return {m.old_id: m.new_id for m in plan.merges}


def _alias_map(plan) -> dict[str, int]:
    return {a.alias_text: a.ingredient_id for a in plan.aliases}


def test_accent_variant_folds_into_slug() -> None:
    # "azúcar" and "azucar" are the same ingredient; the accent-free slug survives (rule c).
    plan = build_consolidation_plan([(3, "azucar"), (4, "azúcar")])
    assert _merge_map(plan) == {4: 3}
    # The redundant alias (the survivor's own normalized name) is not recorded; a merge with a
    # distinct spelling would be — here both normalize to "azucar", so no alias is needed.
    assert "azucar" not in _alias_map(plan)


def test_plural_variant_folds_and_survivor_uses_active_mapping() -> None:
    # "aceituna" (singular) and "aceitunas" (plural slug) group; the plural has the active
    # mapping, so priority (b) selects it as survivor even though neither is a _SPECS key.
    plan = build_consolidation_plan(
        [(7, "aceituna"), (8, "aceitunas")],
        active_mapping_ingredient_ids={8},
    )
    assert _merge_map(plan) == {7: 8}
    assert _alias_map(plan) == {"aceituna": 8}


def test_underscore_slug_survives_spaced_variant() -> None:
    # "pimiento rojo" (spaced) folds into the underscore slug "pimiento_rojo" (rule c).
    plan = build_consolidation_plan([(5, "pimiento_rojo"), (6, "pimiento rojo")])
    assert _merge_map(plan) == {6: 5}
    assert _alias_map(plan) == {"pimiento rojo": 5}


def test_de_in_middle_folds_via_spec_key_priority() -> None:
    # "aceite de oliva" folds onto "aceite_oliva"; the slug is a _SPECS key (priority a),
    # so it survives regardless of id order.
    plan = build_consolidation_plan([(20, "aceite de oliva"), (21, "aceite_oliva")])
    assert _merge_map(plan) == {20: 21}
    assert _alias_map(plan) == {"aceite de oliva": 21}


def test_spec_key_priority_beats_active_mapping_and_id() -> None:
    # Even if the *variant* has the active mapping and the lower id, the exact _SPECS key wins.
    plan = build_consolidation_plan(
        [(1, "aceite de oliva"), (99, "aceite_oliva")],
        active_mapping_ingredient_ids={1},
    )
    assert _merge_map(plan) == {1: 99}


def test_exact_slug_already_ok_is_not_merged() -> None:
    # A lone, already-canonical ingredient is left untouched (no merge, listed as unmerged).
    plan = build_consolidation_plan([(9, "sal"), (10, "aceite_girasol")])
    assert plan.merges == ()
    assert plan.aliases == ()
    assert plan.unmerged_groups == ((9,), (10,))


def test_distinct_ingredients_do_not_merge() -> None:
    # Olive oil and sunflower oil share a token but are different ingredients — never merged.
    plan = build_consolidation_plan([(1, "aceite_oliva"), (2, "aceite_girasol")])
    assert plan.merges == ()


def test_survivor_tie_breaks_on_smallest_id() -> None:
    # Two variants with no distinguishing signal fall to priority (d): smallest id survives.
    plan = build_consolidation_plan([(50, "gambas"), (40, "gamba")])
    assert _merge_map(plan) == {50: 40}


def test_plan_is_deterministic_for_same_input() -> None:
    rows = [
        (1, "aceite_oliva"), (2, "aceite de oliva"),
        (3, "azucar"), (4, "azúcar"),
        (7, "aceituna"), (8, "aceitunas"),
        (9, "sal"),
    ]
    first = build_consolidation_plan(rows, active_mapping_ingredient_ids={8})
    second = build_consolidation_plan(rows, active_mapping_ingredient_ids={8})
    assert first == second


def test_reapplying_plan_on_consolidated_rows_is_a_noop() -> None:
    rows = [
        (1, "aceite_oliva"), (2, "aceite de oliva"),
        (3, "azucar"), (4, "azúcar"),
        (7, "aceituna"), (8, "aceitunas"),
        (9, "sal"),
    ]
    plan = build_consolidation_plan(rows, active_mapping_ingredient_ids={8})
    merged_away = {m.old_id for m in plan.merges}
    remaining = [(i, n) for i, n in rows if i not in merged_away]

    replan = build_consolidation_plan(remaining, active_mapping_ingredient_ids={8})
    assert replan.merges == ()


def test_multiple_variants_fold_into_single_survivor() -> None:
    # Three spellings of the same thing collapse to one survivor with two folds + two aliases.
    plan = build_consolidation_plan(
        [(10, "aceite_oliva"), (11, "aceite de oliva"), (12, "aceite de oliva virgen extra")]
    )
    assert _merge_map(plan) == {11: 10, 12: 10}
    assert _alias_map(plan) == {
        "aceite de oliva": 10,
        "aceite de oliva virgen extra": 10,
    }
