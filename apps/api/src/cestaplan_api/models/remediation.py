"""Durable audit for the reversible history-lane remediation executor (apply spec §2).

Two append-only tables record every remediation run and every per-row change so an apply can be
verified, reproduced and EXACTLY restored. They never store URLs, payloads, secrets or commercial
data — only sanitized identifiers, temporal state and content hashes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.models.base import BaseModel, enum_col

# Execution modes and lifecycle states — kept as VARCHAR+CHECK enums (no CREATE TYPE churn).
REMEDIATION_MODES = ("verify_only", "simulate", "apply", "restore")
REMEDIATION_STATUS = (
    "pending", "verified", "simulated", "applied", "failed", "restored", "rolled_back",
)
REMEDIATION_RESTORE_STATUS = ("none", "restored", "restore_failed", "manual_review_required")
REMEDIATION_CHANGE_STATUS = ("planned", "applied", "restored", "failed")


class HistoryRemediationRun(BaseModel):
    """One execution of the remediation executor against a sealed plan (apply spec §2).

    ``plan_hash`` is UNIQUE: a given sealed plan can complete at most once (idempotency, §9).
    """

    __tablename__ = "history_remediation_run"
    __table_args__ = (
        Index("ix_history_remediation_run_plan_hash", "plan_hash"),
        Index("ix_history_remediation_run_status", "status"),
        # A sealed plan can COMPLETE (status='applied') at most once (idempotency, §9); failed
        # retries share the plan_hash freely.
        Index(
            "uq_history_remediation_run_applied_plan", "plan_hash", unique=True,
            postgresql_where=text("status = 'applied'"),
        ),
    )

    plan_hash: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    planner_tool_version: Mapped[str] = mapped_column(Text, nullable=False)
    planner_source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    writer_contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    main_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    deployed_api_sha: Mapped[str | None] = mapped_column(Text)
    deployed_worker_sha: Mapped[str | None] = mapped_column(Text)
    alembic_revision: Mapped[str] = mapped_column(Text, nullable=False)
    execution_mode: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_MODES, name="history_remediation_mode"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_STATUS, name="history_remediation_status"),
        nullable=False, server_default="pending",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator_reference: Mapped[str | None] = mapped_column(Text)  # sanitized, no secrets/PII
    backup_sha256: Mapped[str | None] = mapped_column(Text)
    before_counts: Mapped[dict | None] = mapped_column(JSONB)
    after_counts: Mapped[dict | None] = mapped_column(JSONB)
    execution_hash: Mapped[str | None] = mapped_column(Text)
    restore_status: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_RESTORE_STATUS, name="history_remediation_restore_status"),
        nullable=False, server_default="none",
    )
    # Link a retry run to the failed run it supersedes (§9), without mutating the earlier row.
    supersedes_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("history_remediation_run.id")
    )


class HistoryRemediationChange(BaseModel):
    """One per-row change proposed/applied/restored by a remediation run (apply spec §2)."""

    __tablename__ = "history_remediation_change"
    __table_args__ = (
        Index("ix_history_remediation_change_run", "remediation_run_id"),
        Index("ix_history_remediation_change_observation", "price_observation_id"),
    )

    remediation_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("history_remediation_run.id"), nullable=False
    )
    deterministic_action_id: Mapped[str] = mapped_column(Text, nullable=False)
    # A plain reference by id (NOT a DB foreign key): a real incoming FK to price_observation.id
    # would register as a new domain FK the remediation planner must handle. PriceObservation is
    # never deleted, so integrity holds without a constraint — and the planner stays untouched.
    price_observation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    original_temporal_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expected_bound_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actual_after_state: Mapped[dict | None] = mapped_column(JSONB)
    original_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expected_bound_hash: Mapped[str] = mapped_column(Text, nullable=False)
    actual_after_hash: Mapped[str | None] = mapped_column(Text)
    restore_state: Mapped[dict | None] = mapped_column(JSONB)
    created_anomaly_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("price_anomaly.id")
    )
    status: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_CHANGE_STATUS, name="history_remediation_change_status"),
        nullable=False, server_default="planned",
    )
    error_code: Mapped[str | None] = mapped_column(Text)  # sanitized code, never a raw message
