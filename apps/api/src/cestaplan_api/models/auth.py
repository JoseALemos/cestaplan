"""Identity and session models: User, UserSession."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cestaplan_api.models.base import BaseModel, enum_col

if TYPE_CHECKING:
    from cestaplan_api.models.household import Household, HouseholdMember

USER_STATUS = ("active", "suspended", "anonymized")


class User(BaseModel):
    """Person account. Holds personal data subject to minimisation and deletion."""

    __tablename__ = "user"
    __table_args__ = (Index("ux_user_email", "email", unique=True),)

    # NOTE: docs/DATA_MODEL.md specifies citext for case-insensitive email uniqueness.
    # The citext extension needs superuser to install and is unavailable on this DB,
    # so email is stored as text with a unique index; callers normalise to lowercase.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default="es-ES")
    ai_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        enum_col(*USER_STATUS, name="user_status"),
        nullable=False,
        server_default="active",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    memberships: Mapped[list[HouseholdMember]] = relationship(
        back_populates="user",
        foreign_keys="HouseholdMember.user_id",
    )
    owned_households: Mapped[list[Household]] = relationship(
        back_populates="owner",
        foreign_keys="Household.owner_user_id",
    )


class UserSession(BaseModel):
    """Opaque server-side session. Stores a hash of the token, never the raw token."""

    __tablename__ = "user_session"
    __table_args__ = (
        Index("ux_session_token_hash", "token_hash", unique=True),
        Index(
            "ix_session_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    user_agent: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="sessions")
