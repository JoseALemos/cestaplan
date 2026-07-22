"""Request schemas for plan generation, regeneration and recipe feedback.

Responses are assembled as plain dicts by :mod:`cestaplan_api.services.plan_service`
with all money serialized as strings, so only inputs are modelled here.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

MealType = Literal["breakfast", "lunch", "snack", "dinner"]
Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
FeedbackSentiment = Literal["like", "reject", "no_show"]

_MAX_REQUIREMENTS = 4
_MAX_COUNT = 100


class MealRequirementIn(BaseModel):
    meal_type: MealType
    requested_count: int = Field(ge=0, le=_MAX_COUNT)
    default_servings: int = Field(default=1, ge=1, le=50)
    selected_dates: list[date] | None = None
    auto_distribute: bool = True
    preferred_days: list[Weekday] | None = None
    maximum_preparation_minutes: int | None = Field(default=None, ge=0, le=1440)
    requires_tupper: bool = False
    reheating_available: bool = True

    def to_row(self) -> dict:
        return {
            "meal_type": self.meal_type,
            "requested_count": self.requested_count,
            "default_servings": self.default_servings,
            "selected_dates": (
                [d.isoformat() for d in self.selected_dates]
                if self.selected_dates is not None
                else None
            ),
            "auto_distribute": self.auto_distribute,
            "preferred_days": self.preferred_days,
            "maximum_preparation_minutes": self.maximum_preparation_minutes,
            "requires_tupper": self.requires_tupper,
            "reheating_available": self.reheating_available,
        }


class GenerateRequest(BaseModel):
    household_id: uuid.UUID
    start_date: date
    end_date: date
    budget_amount: Decimal = Field(ge=0, le=1_000_000)
    # How to use the budget: "waste" (default) maximizes variety within the budget;
    # "price" minimizes cost (the cheapest plan). Preserves current behavior by default.
    priority: Literal["price", "waste"] = "waste"
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    # The chain (retailer) whose prices this plan is costed against. Prices are aggregated
    # across ALL of the chain's stores (the specific store is irrelevant) and never mixed
    # across chains. When omitted the household's default chain is used.
    retailer_id: uuid.UUID | None = None
    # Deprecated in favour of ``retailer_id`` but still accepted for backward compatibility:
    # a store only resolves the chain it belongs to. When both are given ``retailer_id`` wins.
    store_id: uuid.UUID | None = None
    requirements: list[MealRequirementIn] = Field(min_length=1, max_length=_MAX_REQUIREMENTS)

    @model_validator(mode="after")
    def _check(self) -> GenerateRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if sum(r.requested_count for r in self.requirements) == 0:
            raise ValueError("at least one meal must be requested")
        return self


class FeedbackRequest(BaseModel):
    sentiment: FeedbackSentiment


class GroceryItemIn(BaseModel):
    ingredient_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    generic_name: str = Field(min_length=1, max_length=200)
    needed_quantity: Decimal = Field(gt=0, le=1_000_000)
    unit: str = Field(min_length=1, max_length=20)


class SubstituteRequest(BaseModel):
    product_id: uuid.UUID
