"""Fase 2 spec tests: the high-frequency recipe ingredients added to ``_SPECS``.

Same contract as ``test_ingredient_dictionary``: required terms must be present, excluding /
forbidden terms reject the wrong product, a wrong unit blocks auto-approval, and — crucially — a
generic spec never swallows its specific sibling (nor vice-versa), neither at classify time nor at
Fase-1 consolidation time.
"""

from __future__ import annotations

from cestaplan_api.services.ingredient_consolidation import build_consolidation_plan
from cestaplan_api.services.ingredient_dictionary import classify_mapping


def _c(key: str, name: str, **kw: object):
    return classify_mapping(key, product_name=name, **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Per-spec: required present -> matches; excluding/forbidden -> incompatible.
# --------------------------------------------------------------------------- #
def test_huevo_matches_with_category_and_rejects_dangerous_forms() -> None:
    ok = _c("huevo", "Huevos frescos M docena", category_code="huevos", net_content_unit="unit")
    assert ok.mapping_status == "auto_approved"
    assert _c("huevo", "Huevo hilado 100 g", net_content_unit="g").mapping_status == "incompatible"
    assert _c("huevo", "Huevo líquido pasteurizado 1 L",
              net_content_unit="l").mapping_status == "incompatible"


def test_generic_aceite_matches_oil_but_rejects_non_food() -> None:
    ok = _c("aceite", "Aceite de girasol 1 L", category_code="aceites_condimentos",
            net_content_unit="l")
    assert ok.mapping_status == "auto_approved"
    assert _c("aceite", "Aceite de motor 5W30 5 L",
              net_content_unit="l").mapping_status == "incompatible"


def test_harina_rejects_other_grain_flours() -> None:
    ok = _c("harina", "Harina de trigo 1 kg", category_code="panaderia_reposteria",
            net_content_unit="kg")
    assert ok.mapping_status == "auto_approved"
    assert _c("harina", "Harina de maíz 1 kg",
              net_content_unit="kg").mapping_status == "incompatible"


def test_pollo_rejects_processed_forms() -> None:
    ok = _c("pollo", "Pechuga de pollo 1 kg", category_code="carnes", net_content_unit="kg")
    assert ok.mapping_status == "auto_approved"
    assert _c("pollo", "Nuggets de pollo 400 g",
              net_content_unit="g").mapping_status == "incompatible"


def test_vino_blanco_multiterm_autoapproves_and_rejects_tinto() -> None:
    ok = _c("vino blanco", "Vino blanco verdejo 750 ml", net_content_unit="ml")
    assert ok.mapping_status == "auto_approved"  # 2 required terms -> specific
    assert _c("vino blanco", "Vino tinto crianza 750 ml",
              net_content_unit="ml").mapping_status == "incompatible"


def test_carne_picada_multiterm_autoapproves_and_rejects_vegan() -> None:
    ok = _c("carne picada", "Carne picada mixta 500 g", net_content_unit="g")
    assert ok.mapping_status == "auto_approved"
    assert _c("carne picada", "Picada vegana de soja 300 g",
              net_content_unit="g").mapping_status == "incompatible"


def test_wrong_unit_blocks_auto_approval() -> None:
    # A perfect name match with an incompatible unit must NOT auto-approve; it needs review.
    c = _c("carne picada", "Carne picada 500 g", net_content_unit="l")
    assert c.mapping_status != "auto_approved"
    assert c.required_review is True


# --------------------------------------------------------------------------- #
# No-confusion between a generic spec and its specific sibling (classify time).
# --------------------------------------------------------------------------- #
def test_generic_pan_rejects_breadcrumbs_but_specific_accepts() -> None:
    assert _c("pan", "Pan rallado 500 g", net_content_unit="g").mapping_status == "incompatible"
    ok = _c("pan rallado", "Pan rallado 500 g", net_content_unit="g")
    assert ok.mapping_status == "auto_approved"


def test_pimiento_pimienta_pimenton_do_not_cross_match() -> None:
    # pepper (vegetable) vs pepper-spice vs paprika: each rejects the others.
    assert _c("pimiento", "Pimienta negra molida 50 g",
              net_content_unit="g").mapping_status == "incompatible"
    assert _c("pimienta", "Pimiento rojo asado 300 g",
              net_content_unit="g").mapping_status == "incompatible"
    assert _c("pimenton", "Pimiento verde fresco",
              net_content_unit="g").mapping_status == "incompatible"


def test_generic_leche_rejects_plant_drinks() -> None:
    assert _c("leche", "Bebida de avena 1 L", net_content_unit="l").mapping_status == "incompatible"
    ok = _c("leche", "Leche entera 1 L", category_code="lacteos", net_content_unit="l")
    assert ok.mapping_status == "auto_approved"  # whole cow milk is still 'leche'


# --------------------------------------------------------------------------- #
# No-confusion at Fase-1 consolidation: distinct ingredients must NOT be merged.
# --------------------------------------------------------------------------- #
def test_generic_and_specific_ingredients_are_not_merged() -> None:
    rows = [
        (1, "aceite"), (2, "aceite_oliva"),
        (3, "pimiento"), (4, "pimiento_rojo"),
        (5, "leche"), (6, "leche entera"),
        (7, "pan"), (8, "pan rallado"),
        (9, "caldo"), (10, "caldo de pescado"),
        (11, "pimienta"), (12, "pimenton"),
        (13, "harina"), (14, "maicena"),
        (15, "tocino"), (16, "panceta"),
        (17, "garbanzo"), (18, "garbanzos cocidos"),
    ]
    plan = build_consolidation_plan(rows)
    survivor = {m.old_id: m.new_id for m in plan.merges}

    def final(ingredient_id: int) -> int:
        return survivor.get(ingredient_id, ingredient_id)

    must_stay_distinct = [
        (1, 2), (3, 4), (5, 6), (7, 8), (9, 10),
        (11, 12), (11, 3), (12, 3), (13, 14),
        (15, 16),  # tocino vs panceta (alias intentionally dropped)
        (17, 18),  # dry garbanzo vs canned/cooked (alias intentionally dropped)
    ]
    for a, b in must_stay_distinct:
        assert final(a) != final(b), f"{rows[a-1][1]!r} wrongly merged with {rows[b-1][1]!r}"
