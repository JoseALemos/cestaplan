"""Household and dietary-profile models.

Household, HouseholdMember, DietaryProfile, Allergy, FoodPreference, Equipment.
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
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cestaplan_api.models.base import BaseModel, enum_col

if TYPE_CHECKING:
    from cestaplan_api.models.auth import User
    from cestaplan_api.models.catalog import Retailer, Store

MEMBER_ROLE = ("owner", "editor", "viewer")
# An invitation can only grant a non-owner role: ownership is not transferable by invite.
INVITATION_ROLE = ("editor", "viewer")
INVITATION_STATUS = ("pending", "accepted", "revoked", "expired")


class Household(BaseModel):
    """Cohabitation unit. Scope of permissions and of all planning."""

    __tablename__ = "household"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False
    )
    default_retailer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("retailer.id")
    )
    default_store_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("store.id"))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="EUR")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship(
        back_populates="owned_households", foreign_keys=[owner_user_id]
    )
    default_retailer: Mapped[Retailer | None] = relationship(foreign_keys=[default_retailer_id])
    default_store: Mapped[Store | None] = relationship(foreign_keys=[default_store_id])
    members: Mapped[list[HouseholdMember]] = relationship(
        back_populates="household", cascade="all, delete-orphan"
    )
    dietary_profiles: Mapped[list[DietaryProfile]] = relationship(
        back_populates="household", cascade="all, delete-orphan"
    )
    equipment: Mapped[list[Equipment]] = relationship(
        back_populates="household", cascade="all, delete-orphan"
    )


class HouseholdMember(BaseModel):
    """Membership of a User in a Household with a role; also models non-user eaters."""

    __tablename__ = "household_member"
    __table_args__ = (
        Index(
            "ux_member_household_user",
            "household_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index("ix_member_user", "user_id"),
    )

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    role: Mapped[str] = mapped_column(
        enum_col(*MEMBER_ROLE, name="member_role"), nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    is_eater: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Per-member serving multiplier: 1.0 = a standard adult portion. Scales demand.
    relative_serving: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.0"), server_default=text("1.0")
    )
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    household: Mapped[Household] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship(
        back_populates="memberships", foreign_keys=[user_id]
    )
    dietary_profiles: Mapped[list[DietaryProfile]] = relationship(
        back_populates="household_member"
    )


class DietaryProfile(BaseModel):
    """Dietary profile for a household member (or the household). Sensitive data."""

    __tablename__ = "dietary_profile"
    __table_args__ = (Index("ix_profile_household", "household_id"),)

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    household_member_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("household_member.id")
    )
    diet_type: Mapped[str | None] = mapped_column(Text)
    energy_target_kcal: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    protein_target_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    carb_target_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    fat_target_g: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    household: Mapped[Household] = relationship(back_populates="dietary_profiles")
    household_member: Mapped[HouseholdMember | None] = relationship(
        back_populates="dietary_profiles"
    )
    allergies: Mapped[list[Allergy]] = relationship(
        back_populates="dietary_profile", cascade="all, delete-orphan"
    )
    food_preferences: Mapped[list[FoodPreference]] = relationship(
        back_populates="dietary_profile", cascade="all, delete-orphan"
    )


class Allergy(BaseModel):
    """Declared allergen. HARD constraint: validation is deterministic, never the LLM."""

    __tablename__ = "allergy"
    __table_args__ = (Index("ix_allergy_profile", "dietary_profile_id"),)

    dietary_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("dietary_profile.id", ondelete="CASCADE"), nullable=False
    )
    allergen_code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        enum_col("intolerance", "allergy", "anaphylaxis", name="allergy_severity"),
        nullable=False,
    )
    avoid_traces: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    dietary_profile: Mapped[DietaryProfile] = relationship(back_populates="allergies")


class FoodPreference(BaseModel):
    """Soft preference (likes/dislikes). Never overrides a hard restriction."""

    __tablename__ = "food_preference"

    dietary_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("dietary_profile.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(
        enum_col("ingredient", "cuisine", "tag", name="preference_subject_type"),
        nullable=False,
    )
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    sentiment: Mapped[str] = mapped_column(
        enum_col("like", "dislike", "avoid", name="preference_sentiment"),
        nullable=False,
    )
    weight: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))

    dietary_profile: Mapped[DietaryProfile] = relationship(back_populates="food_preferences")


class Equipment(BaseModel):
    """Kitchen equipment available in the household (conditions viable recipes)."""

    __tablename__ = "equipment"

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    equipment_code: Mapped[str] = mapped_column(Text, nullable=False)
    available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    household: Mapped[Household] = relationship(back_populates="equipment")


class HouseholdInvitation(BaseModel):
    """Pending invitation for a real ``User`` to join a household with a role.

    Mirrors the opaque-session pattern: only the SHA-256 hash of the raw token is
    stored (``token_hash``); the raw token is returned to the inviting owner exactly
    once so they can share the accept link manually (no email is sent, matching the
    documented password-recovery stub). Acceptance requires the logged-in user's email
    to match ``email``. A partial unique index enforces at most one pending invitation
    per (household, email).
    """

    __tablename__ = "household_invitation"
    __table_args__ = (
        Index("ux_invitation_token_hash", "token_hash", unique=True),
        Index(
            "ux_invitation_pending",
            "household_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_invitation_household", "household_id"),
    )

    household_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("household.id", ondelete="CASCADE"), nullable=False
    )
    # Stored normalised to lowercase; the accepting user's email must match this.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(
        enum_col(*INVITATION_ROLE, name="invitation_role"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_col(*INVITATION_STATUS, name="invitation_status"),
        nullable=False,
        server_default="pending",
    )
    invited_by_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id")
    )

    household: Mapped[Household] = relationship(foreign_keys=[household_id])
