"""Audit trail model: AuditLog."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.models.base import BaseModel


class AuditLog(BaseModel):
    """Audit record of sensitive actions (admin, data changes, access)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_public_id"),
        Index("ix_audit_actor_time", "actor_user_id", "occurred_at"),
    )

    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("user.id"))
    household_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("household.id"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_public_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    audit_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)
    ip_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
