"""Recipe equipment normalization: free-text Spanish terms -> canonical codes."""

from __future__ import annotations

from cestaplan_api.services.equipment_normalize import (
    normalize_equipment,
    unmapped_terms,
)


def test_maps_spanish_terms_to_canonical_codes():
    assert normalize_equipment(["olla"]) == ["pot"]
    assert normalize_equipment(["sarten"]) == ["stovetop"]
    assert normalize_equipment(["plancha"]) == ["griddle"]
    assert normalize_equipment(["horno", "fuente_horno"]) == ["oven"]  # both -> oven, dedup
    assert normalize_equipment(["batidora"]) == ["blender"]
    assert normalize_equipment(["tostadora"]) == ["toaster"]
    assert normalize_equipment(["pasapures"]) == ["food_processor"]


def test_canonical_codes_pass_through_unchanged():
    seed = ["oven", "stovetop", "blender"]
    assert normalize_equipment(seed) == seed


def test_drops_utensils_and_unknown_terms():
    # Knives, bowls, spoons are not conditioning appliances.
    assert normalize_equipment(["cuchillo", "bol", "cuchara", "vaso"]) == []
    # A truly unknown term imposes no phantom requirement (safe direction).
    assert normalize_equipment(["teletransportador"]) == []


def test_dedup_and_order_stable():
    assert normalize_equipment(["olla", "cazo", "sarten"]) == ["pot", "stovetop"]
    assert normalize_equipment(["oven", "oven"]) == ["oven"]


def test_case_insensitive_and_none_safe():
    assert normalize_equipment(["Horno", " OLLA "]) == ["oven", "pot"]
    assert normalize_equipment(None) == []


def test_unmapped_terms_flags_only_the_genuinely_unknown():
    # Canonical, mapped, and recognised utensils are NOT unmapped.
    assert unmapped_terms(["oven", "olla", "cuchillo"]) == []
    # A term we neither map nor recognise as a utensil is surfaced.
    assert unmapped_terms(["teletransportador"]) == ["teletransportador"]
