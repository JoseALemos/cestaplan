"""Pure costing maths + recipe-targeted dictionary rules (spec §6/§9) — no DB, no network."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cestaplan_api.services.ingredient_dictionary import (
    classify_mapping,
    normalize_provider_category,
)
from cestaplan_api.services.recipe_costing import fixed_package_cost, to_base


# --------------------------------------------------------------------------- #
# Unit conversion + package maths
# --------------------------------------------------------------------------- #
def test_to_base_converts_and_tags_dimension() -> None:
    assert to_base(Decimal("1"), "kg") == (Decimal("1000"), "mass")
    assert to_base(Decimal("1"), "l") == (Decimal("1000"), "volume")
    assert to_base(Decimal("80"), "g") == (Decimal("80"), "mass")
    assert to_base(Decimal("2"), "unit") == (Decimal("2"), "count")
    assert to_base(Decimal("1"), "cucharada") is None  # unknown unit


def test_fixed_package_ceils_to_whole_packages() -> None:
    # 400 ml required, 1000 ml pack @0.95 -> exactly one pack, never a fraction.
    got = fixed_package_cost(Decimal("400"), "volume", Decimal("1000"), "ml", Decimal("0.95"))
    assert got == (Decimal("1"), Decimal("1000"), Decimal("0.95"))


def test_fixed_package_buys_two_when_one_is_not_enough() -> None:
    # 1200 ml required, 1000 ml packs -> ceil(1.2) = 2 packages.
    packages, purchased, cost = fixed_package_cost(
        Decimal("1200"), "volume", Decimal("1000"), "ml", Decimal("0.95")
    )
    assert packages == Decimal("2")
    assert purchased == Decimal("2000")
    assert cost == Decimal("1.90")


def test_fixed_package_rejects_incompatible_dimension() -> None:
    # required is mass (g) but the pack is a volume (ml) -> not buyable.
    assert fixed_package_cost(Decimal("80"), "mass", Decimal("1000"), "ml", Decimal("1")) is None


def test_fixed_package_rejects_zero_or_negative_price() -> None:
    assert fixed_package_cost(Decimal("80"), "mass", Decimal("500"), "g", Decimal("0")) is None
    assert fixed_package_cost(Decimal("80"), "mass", Decimal("500"), "g", Decimal("-1")) is None


def test_fixed_package_rejects_missing_net_content() -> None:
    assert fixed_package_cost(Decimal("80"), "mass", None, None, Decimal("1")) is None


def test_kilogram_pack_costs_a_gram_recipe() -> None:
    # 160 g plátano from a 1 kg pack @1.32 -> one pack.
    packages, purchased, cost = fixed_package_cost(
        Decimal("160"), "mass", Decimal("1"), "kg", Decimal("1.32")
    )
    assert (packages, purchased, cost) == (Decimal("1"), Decimal("1000"), Decimal("1.32"))


# --------------------------------------------------------------------------- #
# Provider-category normalisation (enables single-word deterministic approval)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Frutas y verduras", "frutas"),
        ("Frescos > Frutas", "frutas"),
        ("Lácteos", "lacteos"),
        ("Aceite, especias y salsas", "aceites_condimentos"),
        ("Bebidas", None),
        (None, None),
    ],
)
def test_normalize_provider_category(text: str | None, expected: str | None) -> None:
    assert normalize_provider_category(text) == expected


# --------------------------------------------------------------------------- #
# Recipe-targeted §6 compatibility rules
# --------------------------------------------------------------------------- #
def test_leche_entera_multiterm_auto_approves_without_category() -> None:
    c = classify_mapping(
        "leche_entera",
        product_name="Leche Entera de Vaca 1L",
        category_code=None,
        net_content_unit="l",
    )
    assert c.mapping_status == "auto_approved"
    assert c.required_review is False


@pytest.mark.parametrize(
    "name",
    [
        "Bebida de avena",  # vegetal drink, not milk
        "Leche desnatada de vaca",  # wrong fat variant
        "Leche condensada",  # different product
    ],
)
def test_leche_entera_rejects_incompatible(name: str) -> None:
    c = classify_mapping("leche_entera", product_name=name, net_content_unit="l")
    assert c.mapping_status in ("incompatible", "rejected")


def test_platano_single_word_needs_category() -> None:
    without = classify_mapping("platano", product_name="Plátano de Canarias", net_content_unit="g")
    assert without.required_review is True
    with_cat = classify_mapping(
        "platano",
        product_name="Plátano de Canarias",
        category_code=normalize_provider_category("Frutas"),
        net_content_unit="g",
    )
    assert with_cat.mapping_status == "auto_approved"


@pytest.mark.parametrize(
    "name",
    [
        "Batido sabor plátano",  # milkshake
        "Plátano macho para freir",  # plantain (distinct culinary item)
        "TORTOLINES Plátano frito",  # fried snack
        "Chips de plátano deshidratado",  # dried snack
    ],
)
def test_platano_rejects_incompatible(name: str) -> None:
    c = classify_mapping(
        "platano",
        product_name=name,
        category_code=normalize_provider_category("Frutas"),
        net_content_unit="g",
    )
    assert c.mapping_status in ("incompatible", "rejected")


def test_avena_copos_rejects_cereal_that_contains_flakes() -> None:
    # Has both required terms (copos+avena) but is a breakfast cereal -> excluded by §6.
    c = classify_mapping(
        "avena_copos",
        product_name="Cereales de fibra con copos de avena suaves",
        net_content_unit="g",
    )
    assert c.mapping_status in ("incompatible", "rejected")


@pytest.mark.parametrize(
    "name",
    [
        "Bebida de avena",  # oat drink
        "Harina de avena",  # oat flour
        "Galletas de avena",  # oat biscuits
    ],
)
def test_avena_copos_never_auto_approves_non_flakes(name: str) -> None:
    c = classify_mapping("avena_copos", product_name=name, net_content_unit="g")
    assert c.mapping_status != "auto_approved"  # oat drink/flour/biscuits are never costable


def test_avena_copos_accepts_real_flakes() -> None:
    c = classify_mapping(
        "avena_copos", product_name="Copos de avena integrales 500 g", net_content_unit="g"
    )
    assert c.mapping_status == "auto_approved"
