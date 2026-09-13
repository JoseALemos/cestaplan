"""Loader for AI-authored real recipes — flags, idempotency, coverage + resolution reporting.

The 75 canonical ingredients are pre-seeded in the test DB (the loader never creates ingredients),
so recipes referencing them resolve cleanly. Each test runs inside the transactional ``db_session``
fixture (tools/conftest.py) and is rolled back on teardown.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import Recipe, RecipeIngredient, RecipeStep
from cestaplan_api.tools import load_real_recipes as loader

# A minimal, self-contained recipe list (all canonical names are in the seeded 75).
_SAMPLE = [
    {
        "title": "Test crema de calabacín",
        "description": "Crema de prueba.",
        "servings": 4,
        "meal_types": ["lunch", "dinner"],
        "cuisine": "española",
        "preference_tags": ["vegetariano"],
        "preparation_minutes": 10,
        "cooking_minutes": 25,
        "required_equipment": ["olla", "batidora"],
        "leftover_reuse": "Base para pasta.",
        "storage_instructions": "Nevera 3 días.",
        "reheating_instructions": "Fuego medio.",
        "ingredients": [
            {"canonical_name": "calabacin", "display_name": "Calabacín", "quantity": 600,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "patata", "display_name": "Patata", "quantity": 150,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "aceite_oliva", "display_name": "Aceite de oliva", "quantity": 30,
             "unit": "ml", "optional": False, "substitution_group": None},
            {"canonical_name": "sal", "display_name": "Sal", "quantity": 6,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "queso_fresco", "display_name": "Queso fresco", "quantity": 60,
             "unit": "g", "optional": True, "substitution_group": None},
        ],
        "steps": ["Sofríe.", "Cuece.", "Tritura."],
    },
    {
        "title": "Test arroz con pollo",
        "description": "Arroz de prueba.",
        "servings": 4,
        "meal_types": ["lunch"],
        "cuisine": "española",
        "preference_tags": ["alto_en_proteina"],
        "preparation_minutes": 15,
        "cooking_minutes": 35,
        "required_equipment": ["cazuela"],
        "leftover_reuse": "Salteado.",
        "storage_instructions": "Nevera 2 días.",
        "reheating_instructions": "Sartén.",
        "ingredients": [
            {"canonical_name": "arroz_redondo", "display_name": "Arroz redondo", "quantity": 320,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "pollo_pechuga", "display_name": "Pechuga", "quantity": 400,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "cebolla", "display_name": "Cebolla", "quantity": 120,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "sal", "display_name": "Sal", "quantity": 6,
             "unit": "g", "optional": False, "substitution_group": None},
        ],
        "steps": ["Dora.", "Sofríe.", "Cuece."],
    },
]


def _recipe(db: Session, title: str) -> Recipe:
    return db.execute(
        select(Recipe).where(Recipe.title == title, Recipe.is_synthetic.is_(False))
    ).scalar_one()


def _count(db: Session, model, *where) -> int:
    q = select(func.count()).select_from(model)
    for w in where:
        q = q.where(w)
    return int(db.scalar(q) or 0)


# --------------------------------------------------------------------------- #
# flags / provenance
# --------------------------------------------------------------------------- #
def test_creates_recipe_with_review_gate_flags(db_session: Session) -> None:
    loader.load_recipes(db_session, _SAMPLE)
    r = _recipe(db_session, "Test crema de calabacín")
    assert r.origin == "ai_generated"
    assert r.is_synthetic is False
    assert r.is_public is False  # REVIEW GATE — not served on the live rail
    assert r.verification_status == "pending_review"
    assert r.estimation_model == loader.DEFAULT_MODEL_MARKER
    assert r.generated_by == loader.DEFAULT_MODEL_MARKER
    assert r.servings == 4
    assert r.meal_types == ["lunch", "dinner"]
    assert r.cuisine == "española"


def test_creates_ingredients_and_steps_with_ai_estimated_source(db_session: Session) -> None:
    loader.load_recipes(db_session, _SAMPLE)
    r = _recipe(db_session, "Test crema de calabacín")
    ris = db_session.execute(
        select(RecipeIngredient).where(RecipeIngredient.recipe_id == r.id)
    ).scalars().all()
    assert len(ris) == 5  # 4 mandatory + 1 optional, all resolvable
    for ri in ris:
        assert ri.ingredient_id is not None
        assert ri.quantity_source == "ai_estimated"
    aceite = next(ri for ri in ris if ri.canonical_name == "aceite_oliva")
    assert aceite.unit == "ml"  # base unit preserved from the authored line
    assert float(aceite.quantity) == 30.0
    optional = next(ri for ri in ris if ri.canonical_name == "queso_fresco")
    assert optional.optional is True

    steps = db_session.execute(
        select(RecipeStep).where(RecipeStep.recipe_id == r.id).order_by(RecipeStep.step_number)
    ).scalars().all()
    assert [s.step_number for s in steps] == [1, 2, 3]
    assert steps[0].instruction == "Sofríe."


# --------------------------------------------------------------------------- #
# coverage-gap reporting
# --------------------------------------------------------------------------- #
def test_reports_coverage_gaps_for_mandatory_ingredients_outside_safe_set(
    db_session: Session,
) -> None:
    diff = loader.load_recipes(db_session, _SAMPLE)
    by_title = {r.title: r for r in diff.per_recipe}
    # crema: every mandatory ingredient is in the 26-safe set -> no gaps.
    assert by_title["Test crema de calabacín"].coverage_gaps == []
    # arroz con pollo: arroz_redondo + pollo_pechuga are mandatory and outside the safe set.
    assert set(by_title["Test arroz con pollo"].coverage_gaps) == {"arroz_redondo", "pollo_pechuga"}
    assert diff.fully_costable_ready == 1
    assert diff.with_gaps == 1


def test_optional_ingredient_outside_safe_set_is_not_a_gap(db_session: Session) -> None:
    spec = [{
        "title": "Test espinacas salteadas",
        "description": "d", "servings": 2, "meal_types": ["dinner"], "cuisine": "española",
        "preference_tags": [], "preparation_minutes": 5, "cooking_minutes": 10,
        "required_equipment": ["sarten"], "leftover_reuse": "x",
        "storage_instructions": "x", "reheating_instructions": "x",
        "ingredients": [
            {"canonical_name": "espinaca", "display_name": "Espinaca", "quantity": 400,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "aceite_oliva", "display_name": "Aceite", "quantity": 25,
             "unit": "ml", "optional": False, "substitution_group": None},
            {"canonical_name": "sal", "display_name": "Sal", "quantity": 4,
             "unit": "g", "optional": False, "substitution_group": None},
            # garbanzos_cocido is OUTSIDE the safe set but OPTIONAL -> not a gap.
            {"canonical_name": "garbanzos_cocido", "display_name": "Garbanzos", "quantity": 200,
             "unit": "g", "optional": True, "substitution_group": None},
        ],
        "steps": ["Saltea."],
    }]
    diff = loader.load_recipes(db_session, spec)
    assert diff.per_recipe[0].coverage_gaps == []
    assert diff.fully_costable_ready == 1


# --------------------------------------------------------------------------- #
# idempotency
# --------------------------------------------------------------------------- #
def test_second_run_creates_nothing(db_session: Session) -> None:
    first = loader.load_recipes(db_session, _SAMPLE)
    assert first.recipes_created == 2
    counts = (
        _count(db_session, Recipe, Recipe.is_synthetic.is_(False)),
        _count(db_session, RecipeIngredient),
        _count(db_session, RecipeStep),
    )
    second = loader.load_recipes(db_session, _SAMPLE)
    assert second.recipes_created == 0
    assert second.recipes_reused == 2
    assert all(r.created is False for r in second.per_recipe)
    # byte-identical counts after the second run.
    assert counts == (
        _count(db_session, Recipe, Recipe.is_synthetic.is_(False)),
        _count(db_session, RecipeIngredient),
        _count(db_session, RecipeStep),
    )


def test_reused_recipe_is_not_modified(db_session: Session) -> None:
    loader.load_recipes(db_session, _SAMPLE)
    r = _recipe(db_session, "Test crema de calabacín")
    snapshot = (r.id, r.is_public, r.verification_status, r.description)
    loader.load_recipes(db_session, _SAMPLE)
    r2 = _recipe(db_session, "Test crema de calabacín")
    assert (r2.id, r2.is_public, r2.verification_status, r2.description) == snapshot


def test_synthetic_recipe_with_same_title_does_not_block_creation(db_session: Session) -> None:
    # A synthetic recipe squatting the same title must NOT be merged with or block the AI recipe;
    # identity is (is_synthetic=False, title).
    db_session.add(Recipe(household_id=None, origin="seed", is_public=True, is_synthetic=True,
                          title="Test crema de calabacín", description="synthetic", servings=2))
    db_session.flush()
    diff = loader.load_recipes(db_session, _SAMPLE)
    assert diff.recipes_created == 2
    # exactly one NON-synthetic recipe with that title now exists.
    assert _count(db_session, Recipe, Recipe.title == "Test crema de calabacín",
                  Recipe.is_synthetic.is_(False)) == 1


def test_ambiguous_non_synthetic_identity_fails_closed(db_session: Session) -> None:
    for _ in range(2):
        db_session.add(Recipe(household_id=None, origin="ai_generated", is_public=False,
                              is_synthetic=False, title="Test arroz con pollo",
                              description="dup", servings=4))
    db_session.flush()
    try:
        loader.load_recipes(db_session, _SAMPLE)
    except loader.RecipeLoadError as exc:
        assert exc.code == "recipe_identity_ambiguous"
    else:  # pragma: no cover
        raise AssertionError("expected RecipeLoadError for ambiguous identity")


# --------------------------------------------------------------------------- #
# unknown canonical_name is reported, never silently mapped
# --------------------------------------------------------------------------- #
def test_unknown_canonical_name_is_reported_and_not_mapped(db_session: Session) -> None:
    spec = [{
        "title": "Test receta con ingrediente inexistente",
        "description": "d", "servings": 2, "meal_types": ["lunch"], "cuisine": "x",
        "preference_tags": [], "preparation_minutes": 5, "cooking_minutes": 5,
        "required_equipment": [], "leftover_reuse": "x",
        "storage_instructions": "x", "reheating_instructions": "x",
        "ingredients": [
            {"canonical_name": "patata", "display_name": "Patata", "quantity": 300,
             "unit": "g", "optional": False, "substitution_group": None},
            {"canonical_name": "ingrediente_inexistente_xyz", "display_name": "Fantasma",
             "quantity": 100, "unit": "g", "optional": False, "substitution_group": None},
        ],
        "steps": ["Cuece."],
    }]
    diff = loader.load_recipes(db_session, spec)
    res = diff.per_recipe[0]
    assert res.unresolved == ["ingrediente_inexistente_xyz"]
    # the recipe is still created, with only the resolvable line.
    assert res.created is True
    assert res.ingredients_created == 1
    r = _recipe(db_session, "Test receta con ingrediente inexistente")
    names = {ri.canonical_name for ri in db_session.execute(
        select(RecipeIngredient).where(RecipeIngredient.recipe_id == r.id)).scalars()}
    assert names == {"patata"}  # unknown name was NOT silently mapped to some ingredient


# --------------------------------------------------------------------------- #
# the authored batch file loads cleanly (integration against the seeded vocabulary)
# --------------------------------------------------------------------------- #
def test_authored_batch_file_loads_and_resolves(db_session: Session) -> None:
    recipes = json.loads(loader.DEFAULT_RECIPES_PATH.read_text(encoding="utf-8"))
    assert len(recipes) >= 30
    diff = loader.load_recipes(db_session, recipes)
    assert diff.recipes_created == len(recipes)
    # every canonical_name in the authored batch resolves against the seeded 75 (no gaps in vocab).
    assert all(r.unresolved == [] for r in diff.per_recipe)
    # coverage split matches the authoring intent: a solid set of fully-safe recipes + the rest.
    assert diff.fully_costable_ready >= 15
    assert diff.fully_costable_ready + diff.with_gaps == len(recipes)
