"""Recipe favorites and feedback models (feed future plan generations).

FavoriteRecipe boosts a recipe in scoring; RecipeFeedback with sentiment
``reject``/``no_show`` makes the engine treat the recipe as rejected (heavily
penalized, effectively a hard block). Both are scoped to a household + user.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.models.base import BaseModel, enum_col

FEEDBACK_SENTIMENT = ("like", "reject", "no_show")


class FavoriteRecipe(BaseModel):
    """A recipe a household marked as a favorite (positive scoring signal)."""

    __tablename__ = "favorite_recipe"
    __table_args__ = (
        Index(
            "ux_favorite_household_recipe_user",
            "household_id",
            "recipe_id",
            "user_id",
            unique=True,
        ),
        Index("ix_favorite_household", "household_id"),
    )

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
    )


class RecipeFeedback(BaseModel):
    """A household member's feedback on a recipe (like / reject / no_show).

    ``reject`` and ``no_show`` make the engine exclude the recipe from future plans.
    Unique per (household, recipe, user) so re-submitting updates the same row.
    """

    __tablename__ = "recipe_feedback"
    __table_args__ = (
        Index(
            "ux_feedback_household_recipe_user",
            "household_id",
            "recipe_id",
            "user_id",
            unique=True,
        ),
        Index("ix_feedback_household", "household_id"),
    )

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    recipe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("recipe.id", ondelete="CASCADE"), nullable=False
    )
    sentiment: Mapped[str] = mapped_column(
        enum_col(*FEEDBACK_SENTIMENT, name="recipe_feedback_sentiment"), nullable=False
    )
