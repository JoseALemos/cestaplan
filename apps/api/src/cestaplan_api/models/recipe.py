"""Recipe models: Recipe, RecipeIngredient, RecipeStep.

Vertical-slice simplification (docs/DATA_MODEL.md §5): the canonical model versions
recipe content in ``RecipeVersion``. RecipeVersion is *out* of the vertical slice and
is used implicitly as version 1, so here the editable content lives directly on
``Recipe`` and RecipeIngredient/RecipeStep reference ``recipe_id`` instead of
``recipe_version_id``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cestaplan_api.models.base import BaseModel, enum_col

if TYPE_CHECKING:
    from cestaplan_api.models.catalog import Ingredient

RECIPE_ORIGIN = ("seed", "ai_generated", "user", "imported")
# Provenance / verification vocabularies (documented Text sets; not a DB enum so new values don't
# need a migration). An AI-estimated quantity is NEVER "verified" — it stays pending_review until a
# human reviews it.
RECIPE_VERIFICATION_STATUS = ("pending_review", "verified", "rejected")
QUANTITY_SOURCE = ("source_original", "ai_estimated", "manually_verified")


class Recipe(BaseModel):
    """Stable recipe identity carrying (slice) its editable content."""

    __tablename__ = "recipe"

    household_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("household.id")
    )
    origin: Mapped[str] = mapped_column(
        enum_col(*RECIPE_ORIGIN, name="recipe_origin"), nullable=False
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Content fields (canonical: these live on RecipeVersion; merged here for the slice).
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    servings: Mapped[int] = mapped_column(Integer, nullable=False)
    meal_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    cuisine: Mapped[str | None] = mapped_column(Text)
    preference_tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    preparation_minutes: Mapped[int | None] = mapped_column(Integer)
    cooking_minutes: Mapped[int | None] = mapped_column(Integer)
    required_equipment: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    leftover_reuse: Mapped[str | None] = mapped_column(Text)
    storage_instructions: Mapped[str | None] = mapped_column(Text)
    reheating_instructions: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(Text)
    # --- Provenance / verification (additive; never overwrites recipe content) --- #
    source_dataset: Mapped[str | None] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(Text)
    source_license: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # pending_review | verified | rejected — null for recipes that need no review (seed/user).
    verification_status: Mapped[str | None] = mapped_column(Text)
    # LLM used to derive/estimate structure, when any (never implies the data is verified).
    estimation_model: Mapped[str | None] = mapped_column(Text)
    estimation_prompt_version: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )
    steps: Mapped[list[RecipeStep]] = relationship(
        back_populates="recipe", cascade="all, delete-orphan"
    )


class RecipeIngredient(BaseModel):
    """Ingredient required by a recipe, with a deterministic quantity."""

    __tablename__ = "recipe_ingredient"
    __table_args__ = (Index("ix_recipe_ing_recipe", "recipe_id"),)

    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
    )
    ingredient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingredient.id"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    optional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    substitution_group: Mapped[str | None] = mapped_column(Text)
    # --- Quantity provenance (additive; the quantity value itself is never overwritten) --- #
    # source_original | ai_estimated | manually_verified. Imported belenarbizu quantities are
    # ai_estimated (the dataset carried no quantities), so they are NEVER presented as verified.
    quantity_source: Mapped[str | None] = mapped_column(Text)
    quantity_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    verification_status: Mapped[str | None] = mapped_column(Text)

    recipe: Mapped[Recipe] = relationship(back_populates="ingredients")
    ingredient: Mapped[Ingredient] = relationship(back_populates="recipe_ingredients")


class RecipeStep(BaseModel):
    """Ordered preparation step of a recipe."""

    __tablename__ = "recipe_step"
    __table_args__ = (
        Index("ux_recipe_step", "recipe_id", "step_number", unique=True),
    )

    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    recipe: Mapped[Recipe] = relationship(back_populates="steps")
