"""Pantry, meal planning, grocery list and optimization models.

PantryItem, MealPlan, MealRequirement, PlannedMeal, GroceryList, GroceryListItem,
OptimizationRun.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cestaplan_api.models.base import BaseModel, enum_col, money

if TYPE_CHECKING:
    from cestaplan_api.models.jobs import GenerationJob

MEAL_TYPE = ("breakfast", "lunch", "snack", "dinner")
MEAL_PLAN_STATUS = ("draft", "generating", "ready", "failed", "archived")
PLANNED_MEAL_STATUS = ("planned", "accepted", "rejected", "cooked", "regenerating")
COVERAGE_STATUS = ("complete", "high", "partial", "insufficient", "stale", "none")
PRICE_STATUS = ("known", "estimated", "missing", "stale")
OPTIMIZATION_STATUS = (
    "queued",
    "collecting_data",
    "generating_candidates",
    "validating",
    "optimizing",
    "completed",
    "failed",
    "cancelled",
)


class PantryItem(BaseModel):
    """Household pantry stock. Reduces what is pending to buy (PantryCalculator)."""

    __tablename__ = "pantry_item"
    __table_args__ = (
        Index(
            "ix_pantry_household_ingredient",
            "household_id",
            "ingredient_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ingredient.id")
    )
    product_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("product.id"))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MealPlan(BaseModel):
    """Meal plan for a household and a date range."""

    __tablename__ = "meal_plan"
    __table_args__ = (Index("ix_plan_household_status", "household_id", "status"),)

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    retailer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("retailer.id"))
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    budget_amount: Mapped[Decimal | None] = mapped_column(money())
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="EUR")
    status: Mapped[str] = mapped_column(
        enum_col(*MEAL_PLAN_STATUS, name="meal_plan_status"),
        nullable=False,
        server_default="draft",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    requirements: Mapped[list[MealRequirement]] = relationship(
        back_populates="meal_plan", cascade="all, delete-orphan"
    )
    planned_meals: Mapped[list[PlannedMeal]] = relationship(
        back_populates="meal_plan", cascade="all, delete-orphan"
    )
    grocery_list: Mapped[GroceryList | None] = relationship(
        back_populates="meal_plan", cascade="all, delete-orphan"
    )
    optimization_runs: Mapped[list[OptimizationRun]] = relationship(
        back_populates="meal_plan", cascade="all, delete-orphan"
    )


class MealRequirement(BaseModel):
    """A meal need within a plan (flexible meals)."""

    __tablename__ = "meal_requirement"

    meal_plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_plan.id", ondelete="CASCADE"), nullable=False
    )
    meal_type: Mapped[str] = mapped_column(
        enum_col(*MEAL_TYPE, name="requirement_meal_type"), nullable=False
    )
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)
    default_servings: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_dates: Mapped[list | None] = mapped_column(JSONB)
    auto_distribute: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    preferred_days: Mapped[list | None] = mapped_column(JSONB)
    maximum_preparation_minutes: Mapped[int | None] = mapped_column(Integer)
    requires_tupper: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reheating_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    meal_plan: Mapped[MealPlan] = relationship(back_populates="requirements")
    planned_meals: Mapped[list[PlannedMeal]] = relationship(
        back_populates="meal_requirement"
    )


class PlannedMeal(BaseModel):
    """A concrete meal assigned to a recipe within the plan.

    References ``recipe_id`` directly (RecipeVersion is out of the slice).
    """

    __tablename__ = "planned_meal"
    __table_args__ = (Index("ix_planned_plan_date", "meal_plan_id", "scheduled_date"),)

    meal_plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_plan.id", ondelete="CASCADE"), nullable=False
    )
    meal_requirement_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meal_requirement.id")
    )
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recipe.id"), nullable=False
    )
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    meal_type: Mapped[str] = mapped_column(
        enum_col(*MEAL_TYPE, name="planned_meal_type"), nullable=False
    )
    servings: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_col(*PLANNED_MEAL_STATUS, name="planned_meal_status"),
        nullable=False,
        server_default="planned",
    )
    is_batch_cook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    meal_plan: Mapped[MealPlan] = relationship(back_populates="planned_meals")
    meal_requirement: Mapped[MealRequirement | None] = relationship(
        back_populates="planned_meals"
    )


class GroceryList(BaseModel):
    """Grocery list derived from a plan (one per plan, materialised)."""

    __tablename__ = "grocery_list"
    __table_args__ = (Index("ux_grocery_list_plan", "meal_plan_id", unique=True),)

    meal_plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_plan.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="EUR")
    known_cost_amount: Mapped[Decimal | None] = mapped_column(money())
    estimated_cost_amount: Mapped[Decimal | None] = mapped_column(money())
    price_coverage: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    weighted_price_coverage: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    coverage_status: Mapped[str] = mapped_column(
        enum_col(*COVERAGE_STATUS, name="coverage_status"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    meal_plan: Mapped[MealPlan] = relationship(back_populates="grocery_list")
    items: Mapped[list[GroceryListItem]] = relationship(
        back_populates="grocery_list", cascade="all, delete-orphan"
    )


class GroceryListItem(BaseModel):
    """A grocery list line: a product to buy with whole-package computation."""

    __tablename__ = "grocery_list_item"
    __table_args__ = (Index("ix_gli_list", "grocery_list_id"),)

    grocery_list_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("grocery_list.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("product.id"))
    ingredient_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ingredient.id")
    )
    needed_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    pantry_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    pending_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    package_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    package_unit: Mapped[str | None] = mapped_column(Text)
    packages_selected: Mapped[int | None] = mapped_column(Integer)
    purchased_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    used_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    leftover_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_product_price_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_price.id")
    )
    total_cost: Mapped[Decimal | None] = mapped_column(money())
    recipe_attributable_cost: Mapped[Decimal | None] = mapped_column(money())
    marginal_cost: Mapped[Decimal | None] = mapped_column(money())
    price_status: Mapped[str] = mapped_column(
        enum_col(*PRICE_STATUS, name="grocery_price_status"), nullable=False
    )
    is_checked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    grocery_list: Mapped[GroceryList] = relationship(back_populates="items")


class OptimizationRun(BaseModel):
    """Run of the deterministic optimization engine for a plan. Seed-reproducible."""

    __tablename__ = "optimization_run"

    meal_plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meal_plan.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_col(*OPTIMIZATION_STATUS, name="optimization_run_status"),
        nullable=False,
        server_default="queued",
    )
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scoring_config: Mapped[dict | None] = mapped_column(JSONB)
    budget_amount: Mapped[Decimal | None] = mapped_column(money())
    result_summary: Mapped[dict | None] = mapped_column(JSONB)
    infeasibility_report: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    meal_plan: Mapped[MealPlan] = relationship(back_populates="optimization_runs")
    generation_jobs: Mapped[list[GenerationJob]] = relationship(
        back_populates="optimization_run"
    )
