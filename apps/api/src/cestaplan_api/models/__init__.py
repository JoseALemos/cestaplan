"""ORM models for the CestaPlan vertical slice.

Importing this package registers every model on ``Base.metadata`` so that
``from cestaplan_api.models import User, ...`` works and Alembic autogenerate sees
all tables.
"""

from __future__ import annotations

from cestaplan_api.models.audit import AuditLog
from cestaplan_api.models.auth import User, UserSession
from cestaplan_api.models.base import BaseModel, TimestampMixin, UUIDMixin
from cestaplan_api.models.catalog import (
    DataSource,
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductNutrition,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.models.feedback import FavoriteRecipe, RecipeFeedback
from cestaplan_api.models.household import (
    Allergy,
    DietaryProfile,
    Equipment,
    FoodPreference,
    Household,
    HouseholdMember,
)
from cestaplan_api.models.imports import DataImport, ProductBarcode
from cestaplan_api.models.jobs import GenerationJob
from cestaplan_api.models.plan import (
    GroceryList,
    GroceryListItem,
    MealPlan,
    MealRequirement,
    OptimizationRun,
    PantryItem,
    PlannedMeal,
)
from cestaplan_api.models.recipe import Recipe, RecipeIngredient, RecipeStep
from cestaplan_api.models.usage import UsageLedger

__all__ = [
    "Allergy",
    # audit
    "AuditLog",
    # base
    "BaseModel",
    "DataImport",
    "DataSource",
    "DietaryProfile",
    "Equipment",
    # feedback
    "FavoriteRecipe",
    "FoodPreference",
    # jobs
    "GenerationJob",
    "GroceryList",
    "GroceryListItem",
    # household
    "Household",
    "HouseholdMember",
    "Ingredient",
    "IngredientProductMapping",
    "MealPlan",
    "MealRequirement",
    "OptimizationRun",
    # plan
    "PantryItem",
    "PlannedMeal",
    "Product",
    "ProductBarcode",
    "ProductNutrition",
    "ProductPrice",
    # recipe
    "Recipe",
    "RecipeFeedback",
    "RecipeIngredient",
    "RecipeStep",
    # catalog
    "Retailer",
    "Store",
    "TimestampMixin",
    "UUIDMixin",
    # usage / metering
    "UsageLedger",
    # auth
    "User",
    "UserSession",
]
