"""Commercial catalogue and ingredient models.

Retailer, Store, Product, ProductPrice, ProductNutrition, DataSource, Ingredient,
IngredientProductMapping.
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

from cestaplan_api.ingestion.contracts import LegalStatus, enum_values
from cestaplan_api.models.base import BaseModel, enum_col, money

if TYPE_CHECKING:
    from cestaplan_api.models.recipe import RecipeIngredient

# Legal footing under which a data source may be ingested (see LegalStatus).
DATA_SOURCE_LEGAL_STATUS = enum_values(LegalStatus)

# The 9 canonical source_type values (canonical decisions §source_type).
SOURCE_TYPE = (
    "official",
    "authorized_partner",
    "community_connector",
    "open_dataset",
    "admin_import",
    "manual_entry",
    "user_receipt",
    "estimated",
    "demo",
)
AVAILABILITY = ("in_stock", "out_of_stock", "unknown")
VERIFICATION_STATUS = ("unverified", "machine_verified", "human_verified", "disputed")


class Retailer(BaseModel):
    """Supermarket chain."""

    __tablename__ = "retailer"
    __table_args__ = (Index("ux_retailer_slug", "slug", unique=True),)

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    adapter_key: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="ES")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    stores: Mapped[list[Store]] = relationship(back_populates="retailer")


class Store(BaseModel):
    """Physical store (or catalogue point) of a chain. A price belongs to a store."""

    __tablename__ = "store"
    __table_args__ = (
        Index("ix_store_retailer_postal", "retailer_id", "postal_code"),
        Index(
            "ux_store_retailer_external",
            "retailer_id",
            "external_code",
            unique=True,
            postgresql_where=text("external_code IS NOT NULL"),
        ),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    external_code: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    province: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(Text)
    postal_code: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    catalog_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_coverage_hint: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    retailer: Mapped[Retailer] = relationship(back_populates="stores")


class Product(BaseModel):
    """Catalogue article (unit of purchase). Independent of price."""

    __tablename__ = "product"
    __table_args__ = (
        Index(
            "ux_product_retailer_external",
            "retailer_id",
            "external_id",
            unique=True,
            postgresql_where=text("retailer_id IS NOT NULL AND external_id IS NOT NULL"),
        ),
    )

    retailer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("retailer.id"))
    external_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(Text)
    package_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    package_unit: Mapped[str | None] = mapped_column(Text)
    category_code: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    retailer: Mapped[Retailer | None] = relationship()
    prices: Mapped[list[ProductPrice]] = relationship(back_populates="product")
    nutrition: Mapped[ProductNutrition | None] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductPrice(BaseModel):
    """Append-only price observation for a product in a store at an instant.

    History is built by inserting rows; never a destructive UPDATE.
    """

    __tablename__ = "product_price"
    __table_args__ = (
        Index("ix_price_lookup", "store_id", "product_id", "observed_at"),
        Index("ix_price_product_observed", "product_id", "observed_at"),
        Index("ix_price_import", "import_id"),
        Index(
            "ix_price_expiry",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )

    retailer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("retailer.id"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("store.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(money(), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    package_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    package_unit: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    promotion: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str | None] = mapped_column(
        enum_col(*AVAILABILITY, name="price_availability")
    )
    source_type: Mapped[str] = mapped_column(
        enum_col(*SOURCE_TYPE, name="source_type"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    # import_id references a DataImport batch (not part of the vertical slice); kept as a
    # nullable bigint without an FK constraint since the DataImport table is not created here.
    import_id: Mapped[int | None] = mapped_column(BigInteger)
    verification_status: Mapped[str] = mapped_column(
        enum_col(*VERIFICATION_STATUS, name="verification_status"),
        nullable=False,
        server_default="unverified",
    )
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    retailer: Mapped[Retailer] = relationship()
    store: Mapped[Store] = relationship()
    product: Mapped[Product] = relationship(back_populates="prices")


class ProductNutrition(BaseModel):
    """Nutrition and allergen info for a product (1:1). Used for allergen validation."""

    __tablename__ = "product_nutrition"
    __table_args__ = (Index("ux_nutrition_product", "product_id", unique=True),)

    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.id", ondelete="CASCADE"), nullable=False
    )
    basis_quantity: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    basis_unit: Mapped[str] = mapped_column(Text, nullable=False)
    energy_kcal: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    protein_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    carbohydrate_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    sugars_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    fat_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    saturated_fat_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    fiber_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    salt_g: Mapped[Decimal | None] = mapped_column(Numeric(9, 3))
    allergens: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    traces: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    ingredients_text: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(
        enum_col(*SOURCE_TYPE, name="nutrition_source_type"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    product: Mapped[Product] = relationship(back_populates="nutrition")


class DataSource(BaseModel):
    """Registered data source (catalogue, dataset, connector). Traceability anchor."""

    __tablename__ = "data_source"
    __table_args__ = (Index("ux_data_source_slug", "slug", unique=True),)

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(
        enum_col(*SOURCE_TYPE, name="data_source_source_type"), nullable=False
    )
    adapter_key: Mapped[str | None] = mapped_column(Text)
    license_code: Mapped[str | None] = mapped_column(Text)
    attribution_text: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    url: Mapped[str | None] = mapped_column(Text)
    # Compliance metadata for the ingestion subsystem: the legal footing plus when the
    # source's terms of service / robots.txt were last reviewed, and free-text notes.
    legal_status: Mapped[str] = mapped_column(
        enum_col(*DATA_SOURCE_LEGAL_STATUS, name="data_source_legal_status"),
        nullable=False,
        server_default=LegalStatus.UNKNOWN.value,
    )
    terms_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    robots_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class Ingredient(BaseModel):
    """Canonical ingredient used by recipes (brand/store independent)."""

    __tablename__ = "ingredient"
    __table_args__ = (Index("ux_ingredient_canonical", "canonical_name", unique=True),)

    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    category_code: Mapped[str | None] = mapped_column(Text)
    default_unit: Mapped[str | None] = mapped_column(Text)
    density_g_per_ml: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    allergen_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    product_mappings: Mapped[list[IngredientProductMapping]] = relationship(
        back_populates="ingredient"
    )
    recipe_ingredients: Mapped[list[RecipeIngredient]] = relationship(
        back_populates="ingredient"
    )


class IngredientProductMapping(BaseModel):
    """Resolves a recipe ingredient to buyable catalogue products (ProductMatcher core)."""

    __tablename__ = "ingredient_product_mapping"
    __table_args__ = (
        Index("ux_ing_product", "ingredient_id", "product_id", unique=True),
        Index("ix_ing_map_ingredient_rank", "ingredient_id", "preference_rank"),
    )

    # canonical_ingredient_id in the spec: the internal recipe-ingredient this maps to.
    ingredient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingredient.id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.id"), nullable=False
    )
    # The specific sellable variant this mapping resolves to (licensed-feed path). Nullable
    # for legacy demo/import mappings that predate the variant model.
    product_variant_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("product_variant.id")
    )
    retailer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("retailer.id"))
    conversion_factor: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    preference_rank: Mapped[int | None] = mapped_column(Integer)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    # How the mapping was produced (see MATCH_METHODS) and its human/machine review state.
    match_method: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        enum_col(*VERIFICATION_STATUS, name="ing_map_verification_status"),
        nullable=False,
        server_default="unverified",
    )
    verified_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    ingredient: Mapped[Ingredient] = relationship(back_populates="product_mappings")
    product: Mapped[Product] = relationship()
