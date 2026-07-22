"""FASE 5 metering: UsageLedger records REAL server-side token usage (no network).

The OpenAI SDK is never hit — a fake client returns a structured response carrying a
``usage`` object. We assert token counts come from the RESPONSE (never the request), the
seed-fallback path writes NO ledger row, and ``estimated_cost`` is NULL unless a price
table is configured.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.models import Recipe, UsageLedger
from cestaplan_api.services import planning_context
from cestaplan_api.services.candidate_providers import OpenAICandidateProvider
from cestaplan_api.services.usage import (
    compute_estimated_cost,
    extract_token_usage,
    record_openai_usage,
)
from cestaplan_worker.processor import process_job
from tests.worker.factory import enqueue_plan, make_household

# Distinctive token numbers so the assertion can only pass if they came from the
# RESPONSE object (nothing in the request carries these).
_INPUT_TOKENS = 4321
_OUTPUT_TOKENS = 876


class _FakeResponses:
    def __init__(self, output_text: str) -> None:
        self._output_text = output_text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=self._output_text,
            usage=SimpleNamespace(
                input_tokens=_INPUT_TOKENS, output_tokens=_OUTPUT_TOKENS
            ),
        )


class _FakeClient:
    def __init__(self, output_text: str) -> None:
        self.responses = _FakeResponses(output_text)


def _ai_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ai_billing_mode": "byok",
        "openai_api_key": "sk-test",
        "openai_model": "test-model",
        "openai_max_retries": 0,
    }
    base.update(overrides)
    return Settings(**base)


def _provider(client: _FakeClient, settings: Settings) -> OpenAICandidateProvider:
    return OpenAICandidateProvider(settings, client=client, sleep=lambda _s: None)


def _recipe_payload(db: Session) -> str:
    recipes = db.execute(
        select(Recipe).where(Recipe.is_public.is_(True)).order_by(Recipe.id)
    ).scalars().all()
    chosen: dict[str, Recipe] = {}
    for recipe in recipes:
        for mt in recipe.meal_types or []:
            chosen.setdefault(mt, recipe)
    picked = list(dict.fromkeys(chosen.values()))
    out: list[dict[str, Any]] = []
    for recipe in picked:
        out.append(
            {
                "title": f"IA {recipe.title}",
                "description": recipe.description or "",
                "servings": recipe.servings,
                "meal_types": list(recipe.meal_types or []),
                "cuisine": recipe.cuisine or "mediterranea",
                "preference_tags": list(recipe.preference_tags or []),
                "ingredients": [
                    {
                        "canonical_name": ri.canonical_name,
                        "display_name": ri.display_name or ri.canonical_name,
                        "quantity": float(ri.quantity),
                        "unit": ri.unit,
                        "optional": ri.optional,
                        "substitution_group": ri.substitution_group,
                    }
                    for ri in recipe.ingredients
                ],
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


def _ledger_rows(db: Session, household_id: int) -> list[UsageLedger]:
    return list(
        db.execute(
            select(UsageLedger).where(UsageLedger.household_id == household_id)
        ).scalars().all()
    )


# --------------------------------------------------------------------------- #
# REAL usage recorded from the mocked response
# --------------------------------------------------------------------------- #
def test_usage_ledger_records_real_response_tokens(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    settings = _ai_settings()
    client = _FakeClient(_recipe_payload(db_session))
    monkeypatch.setattr(
        planning_context, "get_candidate_provider", lambda _s: _provider(client, settings)
    )

    _meal_plan, run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    rows = _ledger_rows(db_session, household.id)
    assert len(rows) == 1, "exactly one ledger row per OpenAI call"
    row = rows[0]
    # Token counts come from response.usage, NOT the request.
    assert row.input_tokens == _INPUT_TOKENS
    assert row.output_tokens == _OUTPUT_TOKENS
    assert row.model == "test-model"
    assert row.operation == "plan_generation"
    assert row.optimization_run_id == run.id
    assert row.household_id == household.id
    assert row.user_id is None  # worker has no user; household is the key
    # No price table -> cost is never fabricated.
    assert row.estimated_cost is None


# --------------------------------------------------------------------------- #
# Seed-fallback path writes NO ledger row
# --------------------------------------------------------------------------- #
def test_seed_path_writes_no_ledger_row(db_session: Session) -> None:
    # Default settings -> ai_billing_mode=disabled -> SeedCandidateProvider (no OpenAI call).
    _user, household, member = make_household(db_session, allergen="gluten")
    _meal_plan, _run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    assert job.status == "completed", job.last_error
    assert _ledger_rows(db_session, household.id) == []


# --------------------------------------------------------------------------- #
# estimated_cost: NULL without a price table, computed with one
# --------------------------------------------------------------------------- #
def test_extract_token_usage_reads_response_object() -> None:
    response = SimpleNamespace(
        output_text="{}",
        usage=SimpleNamespace(input_tokens=10, output_tokens=3),
    )
    assert extract_token_usage(response) == (10, 3)
    # Missing usage degrades to (0, 0), never raises.
    assert extract_token_usage(SimpleNamespace(output_text="{}")) == (0, 0)


def test_estimated_cost_null_without_price_table() -> None:
    assert compute_estimated_cost("gpt-x", 1_000_000, 1_000_000, {}) is None


def test_estimated_cost_computed_with_price_table() -> None:
    table = {"gpt-x": {"input_per_million": "1.00", "output_per_million": "2.00"}}
    cost = compute_estimated_cost("gpt-x", 1_000_000, 500_000, table)
    assert cost == Decimal("2.00")  # 1*1.00 + 0.5*2.00


def test_record_usage_writes_cost_when_priced(db_session: Session) -> None:
    _user, household, _member = make_household(db_session, allergen=None)
    settings = _ai_settings(
        openai_model="gpt-x",
        openai_price_table=json.dumps(
            {"gpt-x": {"input_per_million": "3.00", "output_per_million": "6.00"}}
        ),
    )
    response = SimpleNamespace(
        output_text="{}",
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    ledger = record_openai_usage(
        db_session,
        response=response,
        settings=settings,
        operation="plan_generation",
        household_id=household.id,
    )
    assert ledger.estimated_cost == Decimal("9.00")  # 1*3 + 1*6
    assert ledger.input_tokens == 1_000_000
    assert ledger.output_tokens == 1_000_000

    stored = db_session.get(UsageLedger, ledger.id)
    assert stored is not None
    assert stored.estimated_cost == Decimal("9.00")


def test_count_of_ledger_rows_per_call_is_one(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, household, member = make_household(db_session, allergen=None)
    settings = _ai_settings()
    client = _FakeClient(_recipe_payload(db_session))
    monkeypatch.setattr(
        planning_context, "get_candidate_provider", lambda _s: _provider(client, settings)
    )
    _meal_plan, _run, job = enqueue_plan(db_session, household, member, budget="500")
    process_job(job, db_session)

    total = db_session.execute(
        select(func.count())
        .select_from(UsageLedger)
        .where(UsageLedger.household_id == household.id)
    ).scalar_one()
    assert total == 1
