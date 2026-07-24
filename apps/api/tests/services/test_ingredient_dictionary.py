"""Ingredient dictionary mapping rules (spec §4/§6) — pure, deterministic, no DB/network.

A shared generic word never auto-approves: single-word ingredients need a confirmed category;
excluding/forbidden terms make a different product incompatible.
"""

from __future__ import annotations

from cestaplan_api.services.ingredient_dictionary import classify_mapping


def _c(key: str, name: str, **kw: object):
    return classify_mapping(key, product_name=name, **kw)  # type: ignore[arg-type]


def test_exact_multi_term_alias_auto_approves() -> None:
    c = _c("aceite_oliva", "Aceite de oliva virgen extra 1 L", net_content_unit="l")
    assert c.mapping_status == "auto_approved"
    assert c.required_review is False and c.confidence >= 0.9


def test_olive_oil_excludes_sunflower() -> None:
    c = _c("aceite_oliva", "Aceite de girasol refinado 1 L", net_content_unit="l")
    assert c.mapping_status == "incompatible"
    assert any("girasol" in w for w in c.warnings)


def test_single_word_generic_needs_review_without_category() -> None:
    # "tomate pera" matches the alias but "tomate" alone is generic -> review, not auto-approve.
    c = _c("tomate", "Tomate pera rama 1 kg", net_content_unit="kg")
    assert c.mapping_status in ("candidate", "ambiguous")
    assert c.required_review is True


def test_single_word_with_confirmed_category_auto_approves() -> None:
    c = _c("tomate", "Tomate pera 1 kg", category_code="verduras", net_content_unit="kg")
    assert c.mapping_status == "auto_approved"


def test_tomate_fresh_vs_crushed() -> None:
    fresh = _c("tomate", "Tomate rama", category_code="verduras", net_content_unit="kg")
    crushed = _c("tomate", "Tomate triturado 400 g", category_code="verduras", net_content_unit="g")
    assert fresh.mapping_status == "auto_approved"
    assert crushed.mapping_status == "incompatible"  # a different product


def test_garlic_vs_garlic_powder() -> None:
    clove = _c("ajo", "Ajos frescos malla", category_code="verduras", net_content_unit="unit")
    powder = _c(
        "ajo", "Ajo en polvo 45 g", category_code="aceites_condimentos", net_content_unit="g"
    )
    assert clove.mapping_status == "auto_approved"
    assert powder.mapping_status == "incompatible"


def test_oat_flakes_vs_oat_drink() -> None:
    flakes = _c("avena_copos", "Copos de avena finos 500 g", net_content_unit="g")
    drink = _c("avena_copos", "Bebida de avena 1 L", net_content_unit="l")
    assert flakes.mapping_status == "auto_approved"  # multi-term specific name
    assert drink.mapping_status == "incompatible"


def test_fresh_spinach_vs_prepared_dish() -> None:
    fresh = _c(
        "espinaca", "Espinaca fresca bolsa 200 g", category_code="verduras", net_content_unit="g"
    )
    dish = _c("espinaca", "Lasaña de espinacas plato preparado", net_content_unit="g")
    assert fresh.mapping_status == "auto_approved"
    assert dish.mapping_status == "incompatible"


def test_incompatible_category_is_rejected() -> None:
    c = _c("sal", "Sal fina", category_code="lacteos", net_content_unit="kg")
    assert c.mapping_status == "incompatible"


def test_salsa_is_not_salt() -> None:
    c = _c("sal", "Salsa de soja 250 ml", net_content_unit="ml")
    assert c.mapping_status == "incompatible"


def test_semantic_score_is_advisory_only() -> None:
    # A high semantic score never approves on its own — rules still gate.
    from decimal import Decimal

    c = _c("tomate", "Tomate frito 350 g", net_content_unit="g", semantic_score=Decimal("0.99"))
    assert c.mapping_status == "incompatible"  # forbidden form wins over semantic score
