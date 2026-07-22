"""Shared declarative base, mixins and column helpers for all ORM models.

Invariants (see docs/DATA_MODEL.md §1):
- Double identity: internal ``bigint`` identity PK ``id`` (never exposed) + public
  ``uuid`` ``public_id`` (stable external identifier). ``public_id`` defaults to
  ``uuid.uuid4`` on the *Python* side; we do NOT rely on a DB function such as
  ``gen_random_uuid()`` because Postgres 12 would need the ``pgcrypto`` extension.
- All timestamps are timezone-aware UTC (``timestamptz``).
- Money and physical quantities are ``Numeric``/``Decimal``, never ``float``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Identity, Numeric, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.db import Base


def money() -> Numeric:
    """Monetary column type: ``numeric(12, 4)`` mapped to :class:`decimal.Decimal`.

    Never use ``Float`` for money. A fresh instance is returned per call so the
    same :class:`~sqlalchemy.types.TypeEngine` object is never shared across columns.
    """
    return Numeric(12, 4)


def enum_col(*values: str, name: str) -> Enum:
    """A VARCHAR + CHECK enumeration (``native_enum=False``).

    Kept non-native to keep Postgres 12 migrations simple (no ``CREATE TYPE`` churn).
    """
    return Enum(*values, name=name, native_enum=False, validate_strings=True)


class UUIDMixin:
    """Public UUID identifier, unique and indexed, generated in Python."""

    public_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        default=uuid.uuid4,
        unique=True,
        index=True,
        nullable=False,
    )


class TimestampMixin:
    """Timezone-aware UTC creation/update audit timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BaseModel(UUIDMixin, TimestampMixin, Base):
    """Abstract base carrying the internal identity PK plus the shared mixins."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
