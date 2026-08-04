"""Candidate recipe providers — the ADR-0004 boundary (``OpenAI proposes; the
deterministic engine validates and calculates``).

Two strategies produce the :class:`CandidateRecipeDTO` set the engine optimizes over:

* :class:`SeedCandidateProvider` — the seeded recipe library filtered to the requested
  meal types (the behaviour used when AI is disabled).
* :class:`OpenAICandidateProvider` — asks the OpenAI **Responses API** for structured
  candidate recipes (JSON Schema, ``strict``), validates and normalizes them, keeps only
  ingredients whose ``canonical_name`` is in the store's allow-list, and **persists** the
  accepted candidates as household-scoped ``Recipe`` rows so the rest of the pipeline (which
  references ``Recipe`` rows by id) works unchanged.

Guarantees (see docs/OPENAI.md):

* The model id is **never** hardcoded: it comes from ``settings.openai_model``.
* The prompt is **pseudonymized**: it carries meal counts, generic soft/hard tags, allergen
  codes, a budget number and equipment codes — never real names, email or internal UUIDs
  (docs/PRIVACY.md §7).
* Any OpenAI failure (timeout after retries, network error, invalid JSON, empty result)
  degrades gracefully to the **seed** recipes with a warning — a plan never fails just
  because OpenAI did. All final decisions (allergies, packages, cost, nutrition) stay in the
  deterministic engine.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Ingredient, Recipe, RecipeIngredient, RecipeStep
from cestaplan_api.services.usage import record_openai_usage
from cestaplan_engine import CandidateRecipeDTO, RecipeIngredientDTO

if TYPE_CHECKING:
    from cestaplan_api.config import Settings

_MEAL_TYPES = ("breakfast", "lunch", "snack", "dinner")
_DEFAULT_PER_MEAL_TYPE = 3
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0


# --------------------------------------------------------------------------- #
# Provider request / result value objects
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CandidateRequest:
    """Everything a provider needs, already pseudonymized by the caller.

    No member alias, email or internal UUID is carried here: only aggregate,
    non-identifying constraints and the store's allow-list of canonical ingredients.
    """

    household_id: int
    requested_types: set[str]
    allow_list: list[str]  # canonical ingredient names permitted by the catalog
    allergens: set[str] = field(default_factory=set)
    hard_restrictions: set[str] = field(default_factory=set)
    soft_preferences: list[str] = field(default_factory=list)
    equipment: set[str] = field(default_factory=set)
    budget_amount: Decimal = Decimal("0")
    currency: str = "EUR"
    requirement_counts: dict[str, int] = field(default_factory=dict)
    per_meal_type: int = _DEFAULT_PER_MEAL_TYPE
    # Metering context (tagged onto UsageLedger; never sent to OpenAI).
    user_id: int | None = None
    operation: str = "plan_generation"
    optimization_run_id: int | None = None


@dataclass(slots=True)
class CandidateBundle:
    """The candidate set plus any non-fatal warnings (e.g. AI fell back to seeds)."""

    candidates: list[CandidateRecipeDTO]
    warnings: list[str] = field(default_factory=list)


class CandidateProvider(Protocol):
    """Strategy that yields the recipe candidates the engine will optimize over."""

    def get_candidates(self, db: Session, request: CandidateRequest) -> CandidateBundle:
        ...


# --------------------------------------------------------------------------- #
# Seed provider (deterministic, no OpenAI)
# --------------------------------------------------------------------------- #
class SeedCandidateProvider:
    """Candidates ARE the seeded recipes filtered to the requested meal types."""

    def get_candidates(self, db: Session, request: CandidateRequest) -> CandidateBundle:
        return CandidateBundle(
            build_seed_candidates(db, request.requested_types, request.allow_list))


def build_seed_candidates(
    db: Session, requested_types: set[str], allow_list: list[str] | None = None
) -> list[CandidateRecipeDTO]:
    """Public seeded recipes filtered to the requested meal types (allergens derived).

    When ``allow_list`` is a non-empty set of canonical ingredient names PRICED by the selected
    retailer (the caller passes the retailer-scoped catalogue), a recipe is offered ONLY when every
    MANDATORY (non-optional) ingredient is priceable by that chain. This keeps the optimizer from
    building a plan out of recipes the chosen chain cannot cost — which would otherwise surface as a
    plan full of "unavailable" line costs. Optional ingredients may be unpriced (mirrors
    ``planner_preflight._count_costable_recipes``). An empty/absent allow_list disables the filter
    (backward-compatible: a retailer with no priced catalogue still yields candidates).
    """
    priceable = set(allow_list) if allow_list else None
    ingredient_allergens = {
        ing_id: set(codes or [])
        for ing_id, codes in db.execute(
            select(Ingredient.id, Ingredient.allergen_codes)
        ).all()
    }

    recipes = db.execute(
        select(Recipe)
        .where(Recipe.deleted_at.is_(None), Recipe.is_public.is_(True))
        .order_by(Recipe.id)
    ).scalars().all()

    candidates: list[CandidateRecipeDTO] = []
    for recipe in recipes:
        meal_types = set(recipe.meal_types or [])
        if requested_types and not (meal_types & requested_types):
            continue

        # Skip recipes the selected chain cannot fully cost (every mandatory ingredient priced).
        if priceable is not None and not all(
            ri.canonical_name in priceable for ri in recipe.ingredients if not ri.optional
        ):
            continue

        ingredients: list[RecipeIngredientDTO] = []
        declared: set[str] = set()
        for ri in recipe.ingredients:
            declared |= ingredient_allergens.get(ri.ingredient_id, set())
            ingredients.append(
                RecipeIngredientDTO(
                    canonical_name=ri.canonical_name,
                    display_name=ri.display_name or ri.canonical_name,
                    quantity=ri.quantity,
                    unit=ri.unit,
                    optional=ri.optional,
                    substitution_group=ri.substitution_group,
                )
            )

        candidates.append(
            CandidateRecipeDTO(
                recipe_id=str(recipe.id),
                title=recipe.title,
                description=recipe.description or "",
                servings=recipe.servings,
                meal_types=meal_types,  # type: ignore[arg-type]
                cuisine=recipe.cuisine or "",
                preference_tags=list(recipe.preference_tags or []),
                ingredients=ingredients,
                steps=[
                    s.instruction
                    for s in sorted(recipe.steps, key=lambda s: s.step_number)
                ],
                preparation_minutes=recipe.preparation_minutes or 0,
                cooking_minutes=recipe.cooking_minutes or 0,
                required_equipment=set(recipe.required_equipment or []),
                allergens_declared=declared,
            )
        )
    return candidates


# --------------------------------------------------------------------------- #
# OpenAI structured-output validation models (docs/OPENAI.md §5)
# --------------------------------------------------------------------------- #
class _OAIIngredient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    display_name: str
    quantity: Decimal = Field(gt=0)
    unit: str
    optional: bool
    substitution_group: str | None


class _OAIRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    servings: int = Field(ge=1)
    meal_types: list[Literal["breakfast", "lunch", "snack", "dinner"]] = Field(min_length=1)
    cuisine: str
    preference_tags: list[str]
    ingredients: list[_OAIIngredient] = Field(min_length=1)
    steps: list[str] = Field(min_length=1)
    preparation_minutes: int = Field(ge=0)
    cooking_minutes: int = Field(ge=0)
    required_equipment: list[str]
    leftover_reuse: str | None
    storage_instructions: str | None
    reheating_instructions: str | None


# The response_format JSON Schema handed to the Responses API (strict structured output).
_RECIPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "description", "servings", "meal_types", "cuisine",
        "preference_tags", "ingredients", "steps", "preparation_minutes",
        "cooking_minutes", "required_equipment", "leftover_reuse",
        "storage_instructions", "reheating_instructions",
    ],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "servings": {"type": "integer", "minimum": 1},
        "meal_types": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "enum": list(_MEAL_TYPES)},
        },
        "cuisine": {"type": "string"},
        "preference_tags": {"type": "array", "items": {"type": "string"}},
        "ingredients": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "canonical_name", "display_name", "quantity",
                    "unit", "optional", "substitution_group",
                ],
                "properties": {
                    "canonical_name": {"type": "string"},
                    "display_name": {"type": "string"},
                    "quantity": {"type": "number", "exclusiveMinimum": 0},
                    "unit": {"type": "string"},
                    "optional": {"type": "boolean"},
                    "substitution_group": {"type": ["string", "null"]},
                },
            },
        },
        "steps": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "preparation_minutes": {"type": "integer", "minimum": 0},
        "cooking_minutes": {"type": "integer", "minimum": 0},
        "required_equipment": {"type": "array", "items": {"type": "string"}},
        "leftover_reuse": {"type": ["string", "null"]},
        "storage_instructions": {"type": ["string", "null"]},
        "reheating_instructions": {"type": ["string", "null"]},
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recipes"],
    "properties": {"recipes": {"type": "array", "items": _RECIPE_SCHEMA}},
}


# --------------------------------------------------------------------------- #
# OpenAI provider
# --------------------------------------------------------------------------- #
def _make_openai_client(settings: Settings) -> Any:
    """Build a thin OpenAI SDK client from settings (import kept local)."""
    from openai import OpenAI

    return OpenAI(
        api_key=settings.openai_api_key,
        timeout=float(settings.openai_timeout_seconds),
        max_retries=0,  # we own the retry/backoff loop below
    )


class OpenAICandidateProvider:
    """Ask OpenAI for structured candidate recipes; validate, filter and persist them.

    Any failure degrades to the seed library with a warning — the plan never fails
    because OpenAI did.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._rng = random.Random(0)

    # -- public API ------------------------------------------------------- #
    def get_candidates(self, db: Session, request: CandidateRequest) -> CandidateBundle:
        try:
            raw = self._invoke_with_retries(db, request)
        except Exception as exc:  # network/timeout/unavailable after retries
            return self._fallback(db, request, f"OpenAI no disponible: {exc}")

        recipes = _parse_recipes(raw)
        if recipes is None:
            return self._fallback(
                db, request, "OpenAI devolvió una respuesta no válida (JSON)"
            )

        allowed = _load_allowed_ingredients(db, request.allow_list)
        candidates = self._persist_accepted(db, request, recipes, allowed)
        if not candidates:
            return self._fallback(
                db, request, "OpenAI no devolvió ninguna receta utilizable"
            )
        return CandidateBundle(candidates)

    # -- OpenAI call with backoff ---------------------------------------- #
    def _invoke_with_retries(self, db: Session, request: CandidateRequest) -> str:
        attempts = max(0, self._settings.openai_max_retries)
        last_exc: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                return self._call_openai(db, request)
            except Exception as exc:  # transient: retry with backoff + jitter
                last_exc = exc
                if attempt >= attempts:
                    break
                delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt))
                self._sleep(delay + self._rng.uniform(0, _BACKOFF_BASE_SECONDS))
        assert last_exc is not None
        raise last_exc

    def _call_openai(self, db: Session, request: CandidateRequest) -> str:
        client = self._client or _make_openai_client(self._settings)
        response = client.responses.create(
            model=self._settings.openai_model,
            reasoning={"effort": self._settings.openai_reasoning_effort},
            input=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _user_prompt(request)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "candidate_recipes",
                    "schema": _RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        )
        # Record REAL usage (server-side truth) for this OpenAI call. A failed cost
        # imputation or ledger write must never break plan generation.
        with contextlib.suppress(Exception):
            record_openai_usage(
                db,
                response=response,
                settings=self._settings,
                operation=request.operation,
                household_id=request.household_id,
                user_id=request.user_id,
                optimization_run_id=request.optimization_run_id,
                currency=request.currency,
            )
        return response.output_text

    # -- persistence ------------------------------------------------------ #
    def _persist_accepted(
        self,
        db: Session,
        request: CandidateRequest,
        recipes: list[_OAIRecipe],
        allowed: dict[str, Ingredient],
    ) -> list[CandidateRecipeDTO]:
        model_id = self._settings.openai_model or None
        candidates: list[CandidateRecipeDTO] = []
        for parsed in recipes:
            kept = [ing for ing in parsed.ingredients if ing.canonical_name in allowed]
            if not kept:
                continue  # every ingredient was outside the allow-list -> drop recipe

            recipe = Recipe(
                household_id=request.household_id,
                origin="ai_generated",
                is_public=False,
                is_synthetic=False,
                title=parsed.title,
                description=parsed.description,
                servings=parsed.servings,
                meal_types=list(parsed.meal_types),
                cuisine=parsed.cuisine,
                preference_tags=list(parsed.preference_tags),
                preparation_minutes=parsed.preparation_minutes,
                cooking_minutes=parsed.cooking_minutes,
                required_equipment=list(parsed.required_equipment) or None,
                leftover_reuse=parsed.leftover_reuse,
                storage_instructions=parsed.storage_instructions,
                reheating_instructions=parsed.reheating_instructions,
                generated_by=model_id,
            )
            db.add(recipe)
            db.flush()  # assign recipe.id

            declared: set[str] = set()
            dto_ingredients: list[RecipeIngredientDTO] = []
            for ing in kept:
                ingredient = allowed[ing.canonical_name]
                # Repair the unit to the ingredient's canonical unit so the deterministic
                # engine resolves quantities against the catalogue without ambiguity.
                unit = ingredient.default_unit or ing.unit
                db.add(
                    RecipeIngredient(
                        recipe_id=recipe.id,
                        ingredient_id=ingredient.id,
                        canonical_name=ingredient.canonical_name,
                        display_name=ing.display_name or ingredient.display_name,
                        quantity=ing.quantity,
                        unit=unit,
                        optional=ing.optional,
                        substitution_group=ing.substitution_group,
                    )
                )
                declared |= set(ingredient.allergen_codes or [])
                dto_ingredients.append(
                    RecipeIngredientDTO(
                        canonical_name=ingredient.canonical_name,
                        display_name=ing.display_name or ingredient.display_name,
                        quantity=ing.quantity,
                        unit=unit,
                        optional=ing.optional,
                        substitution_group=ing.substitution_group,
                    )
                )

            for step_number, instruction in enumerate(parsed.steps, start=1):
                db.add(
                    RecipeStep(
                        recipe_id=recipe.id,
                        step_number=step_number,
                        instruction=instruction,
                    )
                )
            db.flush()

            candidates.append(
                CandidateRecipeDTO(
                    recipe_id=str(recipe.id),
                    title=parsed.title,
                    description=parsed.description,
                    servings=parsed.servings,
                    meal_types=set(parsed.meal_types),  # type: ignore[arg-type]
                    cuisine=parsed.cuisine,
                    preference_tags=list(parsed.preference_tags),
                    ingredients=dto_ingredients,
                    steps=list(parsed.steps),
                    preparation_minutes=parsed.preparation_minutes,
                    cooking_minutes=parsed.cooking_minutes,
                    required_equipment=set(parsed.required_equipment),
                    leftover_reuse=bool(parsed.leftover_reuse),
                    storage_instructions=parsed.storage_instructions,
                    reheating_instructions=parsed.reheating_instructions,
                    allergens_declared=declared,
                )
            )
        return candidates

    # -- fallback --------------------------------------------------------- #
    def _fallback(
        self, db: Session, request: CandidateRequest, reason: str
    ) -> CandidateBundle:
        seeds = build_seed_candidates(db, request.requested_types)
        return CandidateBundle(seeds, warnings=[f"{reason}; usando recetas semilla."])


# --------------------------------------------------------------------------- #
# Parsing / catalogue helpers
# --------------------------------------------------------------------------- #
def _parse_recipes(raw: str) -> list[_OAIRecipe] | None:
    """Parse + validate the model output. ``None`` == unusable (invalid JSON/shape)."""
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("recipes")
    if not isinstance(items, list):
        return None

    recipes: list[_OAIRecipe] = []
    for item in items:
        try:
            recipes.append(_OAIRecipe.model_validate(item))
        except ValidationError:
            continue  # drop the invalid candidate; keep the valid ones
    return recipes


def _load_allowed_ingredients(
    db: Session, allow_list: list[str]
) -> dict[str, Ingredient]:
    if not allow_list:
        return {}
    rows = db.execute(
        select(Ingredient).where(Ingredient.canonical_name.in_(set(allow_list)))
    ).scalars().all()
    return {ing.canonical_name: ing for ing in rows}


# --------------------------------------------------------------------------- #
# Prompts (pseudonymized, docs/PRIVACY.md §7)
# --------------------------------------------------------------------------- #
def _system_prompt() -> str:
    return (
        "Eres un asistente de cocina que propone recetas candidatas para un planificador "
        "de comidas. Devuelves EXCLUSIVAMENTE JSON conforme al esquema proporcionado "
        "(sin texto libre). Reglas estrictas:\n"
        "- Usa SOLO nombres de ingredientes de la lista canónica permitida (canonical_name). "
        "No inventes ingredientes fuera de esa lista.\n"
        "- No decides seguridad de alergias, precios, envases ni nutrición: eso lo calcula "
        "un motor determinista posterior. Tú solo propones recetas.\n"
        "- Respeta las restricciones duras y los alérgenos indicados (no los incluyas)."
    )


def _user_prompt(request: CandidateRequest) -> str:
    counts = ", ".join(
        f"{mt}: {request.requirement_counts.get(mt, 0)}"
        for mt in _MEAL_TYPES
        if mt in request.requested_types
    )
    context = {
        "requested_meal_types": sorted(request.requested_types),
        "requested_counts": counts,
        "candidates_per_meal_type": request.per_meal_type,
        "budget": str(request.budget_amount),
        "currency": request.currency,
        "soft_preferences": sorted(set(request.soft_preferences)),
        "hard_restrictions": sorted(request.hard_restrictions),
        "allergen_codes_to_avoid": sorted(request.allergens),
        "equipment_codes_available": sorted(request.equipment),
        "allowed_canonical_ingredients": sorted(request.allow_list),
    }
    return (
        "Propón recetas candidatas para los siguientes requisitos, "
        f"aproximadamente {request.per_meal_type} por cada tipo de comida solicitado.\n"
        "Contexto (pseudonimizado, sin datos personales):\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def get_candidate_provider(settings: Settings) -> CandidateProvider:
    """OpenAI provider when AI is enabled+configured, else the deterministic seed provider."""
    if settings.ai_enabled:
        return OpenAICandidateProvider(settings)
    return SeedCandidateProvider()
