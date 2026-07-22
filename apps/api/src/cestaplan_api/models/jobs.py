"""Async job queue model: GenerationJob (Postgres-backed, SELECT ... FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cestaplan_api.models.base import BaseModel, enum_col

if TYPE_CHECKING:
    from cestaplan_api.models.plan import OptimizationRun

# Status enum per task spec (aligns with OptimizationRun lifecycle).
JOB_STATUS = (
    "queued",
    "collecting_data",
    "generating_candidates",
    "validating",
    "optimizing",
    "completed",
    "failed",
    "cancelled",
)


class GenerationJob(BaseModel):
    """Async job in the Postgres-backed queue."""

    __tablename__ = "generation_job"
    __table_args__ = (
        # Queue take path (task requirement): filter by status + run_after.
        Index("ix_job_queue", "status", "run_after"),
        Index(
            "ix_job_locked",
            "status",
            "locked_at",
            postgresql_where=text("locked_at IS NOT NULL"),
        ),
    )

    optimization_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("optimization_run.id")
    )
    meal_plan_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("meal_plan.id"))
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_col(*JOB_STATUS, name="generation_job_status"),
        nullable=False,
        server_default="queued",
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    optimization_run: Mapped[OptimizationRun | None] = relationship(
        back_populates="generation_jobs"
    )
