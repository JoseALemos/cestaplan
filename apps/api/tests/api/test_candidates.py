"""Tests for the OpenAI candidate provider (ADR-0004 boundary).

The OpenAI SDK is NEVER hit: a fake client is injected / the provider factory is
monkeypatched. We assert: a valid structured response is parsed, ingredients outside the
allow-list are dropped, accepted candidates are persisted as ``Recipe`` rows, and a full
plan generates from them; an invalid/raised response falls back to seeds with a warning
and the plan still completes; and ``ai_billing_mode=disabled`` uses the seed provider with
no OpenAI call.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import Recipe, RecipeIngredient
from cestaplan_api.services import candidate_providers, planning_context
from cestaplan_api.services.candidate_providers import (
    CandidateRequest,
    OpenAICandidateProvider,
    SeedCandidateProvider,
    get_candidate_provider,
)
from cestaplan_worker.processor import process_job
from tests.worker.factory import enqueue_plan, make_household

_BOGUS = "unobtainium_x"


# --------------------------------------------------------------------------- #
# Fakes + helpers
# --------------------------------------------------------------------------- #
class _FakeResponses:
    def __init__(self, output_text: str | None, error: Exception | None) -> None:
        self._output_text = output_text
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(output_text=self._output_text)


class _FakeClient:
    def __init__(
        self, output_text: str | None = None, error: Exception | None = None
    ) -> None:
        self.responses = _FakeResponses(output_text, error)


def _ai_settings() -> Settings:
    return Settings(
        ai_billing_mode="byok",
        openai_api_key="sk-test",
        openai_model="test-model",
        openai_max_retries=0,
    )


def _provider(client: _FakeClient) -> OpenAICandidateProvider:
    return OpenAICandidateProvider(
        _ai_settings(), client=client, sleep=lambda _s: None
    )


def _recipe_payload(db: Session, *, inject_bogus: bool) -> str:
    """Echo real seeded recipes (one per meal type) as an OpenAI structured response."""
    recipes = db.execute(
        select(Recipe).where(Recipe.is_public.is_(True)).order_by(Recipe.id)
    ).scalars().all()

    chosen: dict[str, Recipe] = {}
    for recipe in recipes:
        for mt in recipe.meal_types or []:
            chosen.setdefault(mt, recipe)
    picked = list(dict.fromkeys(chosen.values()))  # de-dup, stable order

    out: list[dict[str, Any]] = []
    for i, recipe in enumerate(picked):
        ingredients = [
            {
                "canonical_name": ri.canonical_name,
                "display_name": ri.display_name or ri.canonical_name,
                "quantity": float(ri.quantity),
                "unit": ri.unit,
                "optional": ri.optional,
                "substitution_group": ri.substitution_group,
            }
            for ri in recipe.ingredients
        ]
        if inject_bogus and i == 0:
            ingredients.append(
                {
                    "canonical_name": _BOGUS,
                    "display_name": "Materia imposible",
                    "quantity": 10.0,
                    "unit": "g",
                    "optional": False,
                    "substitution_group": None,
                }
            )
        out.append(
            {
                "title": f"IA {recipe.title}",
                "description": recipe.description or "",
                "servings": recipe.servings,
                "meal_types": list(recipe.meal_types or []),
                "cuisine": recipe.cuisine or "mediterranea",
                "preference_tags": list(recipe.preference_tags or []),
                "ingredients": ingredients,
                "steps": [s.instruction for s in recipe.steps] or ["Cocinar."],
                "preparation_minutes": recipe.preparation_minutes or 5,
                "cooking_minutes": recipe.cooking_minutes or 5,
                "required_equipment": list(recipe.required_equipment or []),
                "leftover_reuse": None,
                "storage_instructions": None,
                "reheating_instructions": None,
            }
        )
    return json.dumps({"recipes": out})


def _ai_recipes(db: Session, household_id: int) -> Sequence[Recipe]:
    return db.execute(
        select(Recipe).where(
            Recipe.household_id == household_id,
            Recipe.origin == "ai_generated",
        )
    ).scalars().all()


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def test_factory_selects_provider_by_ai_enabled() -> None:
    assert isinstance(get_candidate_provider(_ai_settings()), OpenAICandidateProvider)
    disabled = Settings(ai_billing_mode="disabled")
    assert isinstance(get_candidate_provider(disabled), SeedCandidateProvider)


# --------------------------------------------------------------------------- #
# Valid response -> parsed, allow-list enforced, persisted, full plan
# --------------------------------------------------------------------------- #
def test_valid_response_persists_candidates_and_generates_plan(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    payload = _recipe_payload(db_session, inject_bogus=True)
    client = _FakeClient(output_text=payload)

    monkeypatch.setattr(
        planning_context, "get_candidate_provider", lambda _s: _provider(client)
    )

    _meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    assert run.status == "completed"

    # The Responses API was called with the configured model + strict json_schema, and the
    # prompt carried NO personal data (pseudonymized context only).
    call = client.responses.calls[0]
    assert call["model"] == "test-model"
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["strict"] is True
    prompt = json.dumps(call["input"])
    assert "Alex" not in prompt and "Sam" not in prompt
    assert "@example.com" not in prompt

    # Accepted candidates were persisted as household-scoped Recipe rows.
    ai = _ai_recipes(db_session, household.id)
    assert ai, "expected persisted OpenAI recipes"
    assert all(r.is_public is False and r.is_synthetic is False for r in ai)
    assert all(r.generated_by == "test-model" for r in ai)

    # The out-of-allow-list ingredient was dropped/repaired away.
    bogus = db_session.execute(
        select(RecipeIngredient).where(RecipeIngredient.canonical_name == _BOGUS)
    ).scalars().all()
    assert bogus == []

    # A full plan generated using them.
    from cestaplan_api.models import PlannedMeal

    meals = db_session.execute(
        select(PlannedMeal).where(PlannedMeal.meal_plan_id == _meal_plan.id)
    ).scalars().all()
    assert len(meals) == 10
    ai_ids = {r.id for r in ai}
    assert all(m.recipe_id in ai_ids for m in meals)


# --------------------------------------------------------------------------- #
# Invalid JSON -> fallback to seeds + warning, plan still completes
# --------------------------------------------------------------------------- #
def test_invalid_json_falls_back_to_seeds_with_warning(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    client = _FakeClient(output_text="not-json {{{")
    monkeypatch.setattr(
        planning_context, "get_candidate_provider", lambda _s: _provider(client)
    )

    _meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    assert run.result_summary is not None
    assert any("semilla" in w for w in run.result_summary["warnings"])
    # No AI recipes were persisted.
    assert _ai_recipes(db_session, household.id) == []


# --------------------------------------------------------------------------- #
# Raised error -> fallback to seeds + warning, plan still completes
# --------------------------------------------------------------------------- #
def test_raised_error_falls_back_to_seeds_with_warning(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, household, member = make_household(db_session, allergen="gluten")
    client = _FakeClient(error=RuntimeError("connection reset"))
    monkeypatch.setattr(
        planning_context, "get_candidate_provider", lambda _s: _provider(client)
    )

    _meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    assert run.result_summary is not None
    assert any("semilla" in w for w in run.result_summary["warnings"])
    assert _ai_recipes(db_session, household.id) == []


# --------------------------------------------------------------------------- #
# Direct provider fallback returns seed candidates + warning
# --------------------------------------------------------------------------- #
def test_provider_fallback_returns_seed_candidates(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    provider = _provider(_FakeClient(error=RuntimeError("timeout")))
    bundle = provider.get_candidates(
        db_session,
        CandidateRequest(
            household_id=household.id,
            requested_types={"lunch"},
            allow_list=["chicken_breast"],
        ),
    )
    assert bundle.candidates, "seed fallback should yield candidates"
    assert bundle.warnings and "semilla" in bundle.warnings[0]


# --------------------------------------------------------------------------- #
# ai_billing_mode=disabled -> seed provider, no OpenAI client is ever built
# --------------------------------------------------------------------------- #
def test_disabled_mode_uses_seed_provider_no_openai_call(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_settings: Any) -> Any:
        raise AssertionError("OpenAI client must not be built when AI is disabled")

    monkeypatch.setattr(candidate_providers, "_make_openai_client", _boom)

    _user, household, member = make_household(db_session, allergen="gluten")
    _meal_plan, _run, job = enqueue_plan(db_session, household, member, budget="500")
    # Default settings have ai_billing_mode=disabled -> SeedCandidateProvider is used.
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    assert _ai_recipes(db_session, household.id) == []
