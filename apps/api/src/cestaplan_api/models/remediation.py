"""Durable audit for the reversible history-lane remediation executor (apply spec §2/§4/§5/§7).

Two append-only tables record every remediation run and every per-row change so an apply can be
verified, reproduced and EXACTLY restored, and a failure or restore-drift leaves a durable record.
They never store URLs, payloads, secrets or commercial data — only sanitized identifiers, temporal
state and content hashes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
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

    A partial-unique index lets a plan COMPLETE (status='applied') at most once while failed retries
    share the plan_hash (idempotency, §9). Provenance is stored as separate expected/observed pairs
    (§1): equality is proven, never assumed from a value's mere presence.
    """

    __tablename__ = "history_remediation_run"
    __table_args__ = (
        Index("ix_history_remediation_run_plan_hash", "plan_hash"),
        Index("ix_history_remediation_run_status", "status"),
        Index(
            "uq_history_remediation_run_applied_plan", "plan_hash", unique=True,
            postgresql_where=text("status = 'applied'"),
        ),
        # A failed run can be superseded by at most one retry (no ambiguous/duplicate links, §2).
        Index("uq_history_remediation_run_supersedes", "supersedes_run_id", unique=True,
              postgresql_where=text("supersedes_run_id IS NOT NULL")),
    )

    plan_hash: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    planner_tool_version: Mapped[str] = mapped_column(Text, nullable=False)
    planner_source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    writer_contract_version: Mapped[str] = mapped_column(Text, nullable=False)
    main_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
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

    # --- Provenance: expected (from a sealed authorization package) vs observed (runtime) (§1) ---
    expected_commit_sha: Mapped[str | None] = mapped_column(Text)
    observed_commit_sha: Mapped[str | None] = mapped_column(Text)
    expected_source_hash: Mapped[str | None] = mapped_column(Text)
    observed_source_hash: Mapped[str | None] = mapped_column(Text)
    expected_api_artifact_hash: Mapped[str | None] = mapped_column(Text)
    observed_api_artifact_hash: Mapped[str | None] = mapped_column(Text)
    expected_worker_artifact_hash: Mapped[str | None] = mapped_column(Text)
    observed_worker_artifact_hash: Mapped[str | None] = mapped_column(Text)
    # Provenance document hash kept as a separate expected/observed pair (§4v4): a restore proves
    # the build's document matches the run's, not merely that a fresh package is self-coherent.
    expected_provenance_document_hash: Mapped[str | None] = mapped_column(Text)
    observed_provenance_document_hash: Mapped[str | None] = mapped_column(Text)

    # --- Backup evidence (§7/§9) — observed values, never a copied expected ---
    backup_sha256: Mapped[str | None] = mapped_column(Text)
    backup_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    backup_postgres_version: Mapped[str | None] = mapped_column(Text)  # expected server version
    backup_pg_restore_version: Mapped[str | None] = mapped_column(Text)  # observed pg_restore
    backup_database_version: Mapped[str | None] = mapped_column(Text)  # observed live server
    backup_dump_database_version: Mapped[str | None] = mapped_column(Text)  # from the dump header
    backup_restore_list_verified: Mapped[bool | None] = mapped_column(Boolean)
    backup_permissions_verified: Mapped[bool | None] = mapped_column(Boolean)
    backup_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sanitized reference only — NEVER a local path, signed URL, credential or token (§4v4). The
    # hash lets two records be compared without re-storing even the sanitized string.
    backup_storage_reference: Mapped[str | None] = mapped_column(Text)  # sanitized or NULL
    backup_storage_reference_hash: Mapped[str | None] = mapped_column(Text)
    backup_evidence_hash: Mapped[str | None] = mapped_column(Text)

    # --- Post-apply evidence for a same-controls restore (§4), sanitized ---
    post_apply_occurrence_hashes: Mapped[dict | None] = mapped_column(JSONB)
    post_apply_supported_fk_hashes: Mapped[dict | None] = mapped_column(JSONB)
    discovered_fk_fingerprint: Mapped[str | None] = mapped_column(Text)
    expected_unknown_fk_count: Mapped[int | None] = mapped_column(BigInteger)

    before_counts: Mapped[dict | None] = mapped_column(JSONB)
    after_counts: Mapped[dict | None] = mapped_column(JSONB)
    execution_hash: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)  # sanitized code on failure, never raw msg
    restore_status: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_RESTORE_STATUS, name="history_remediation_restore_status"),
        nullable=False, server_default="none",
    )
    # Link a retry run to the failed run it supersedes (§2/§5), without mutating the earlier row. A
    # partial-unique index makes each failed run supersedable at most once (no ambiguous chains).
    supersedes_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("history_remediation_run.id")
    )


class HistoryRemediationPlanConsumption(BaseModel):
    """IMMUTABLE record that a plan_hash was applied at least once (apply spec §1).

    ``plan_hash`` is UNIQUE and this row is NEVER deleted — not even by a restore. So a plan that
    has ever been applied can never be applied again: a re-apply after a restore regenerates a fresh
    plan over the restored state. It is separate from the mutable run ``status``.
    """

    __tablename__ = "history_remediation_plan_consumption"
    __table_args__ = (
        Index("uq_history_remediation_plan_consumption", "plan_hash", unique=True),
    )

    plan_hash: Mapped[str] = mapped_column(Text, nullable=False)
    first_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("history_remediation_run.id"), nullable=False
    )
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_hash: Mapped[str | None] = mapped_column(Text)


class HistoryRemediationChange(BaseModel):
    """One per-row change proposed/applied/restored by a remediation run (apply spec §2/§4/§7)."""

    __tablename__ = "history_remediation_change"
    __table_args__ = (
        Index("ix_history_remediation_change_run", "remediation_run_id"),
        Index("ix_history_remediation_change_observation", "price_observation_id"),
        # Deterministic per (run, action) — no duplicate change rows for one action in a run (§4).
        UniqueConstraint("remediation_run_id", "deterministic_action_id",
                         name="uq_history_remediation_change_action"),
    )

    remediation_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("history_remediation_run.id"), nullable=False
    )
    deterministic_action_id: Mapped[str] = mapped_column(Text, nullable=False)
    lane_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)  # sanitized, for re-locking
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
    # Canonical seal over the change's full post-apply evidence (§1v5), computed after post-flush. A
    # restore recomputes and compares it before any write; a tampered target/state fails closed.
    apply_evidence_hash: Mapped[str] = mapped_column(Text, nullable=False)
    restore_state: Mapped[dict | None] = mapped_column(JSONB)
    # Durable anomaly reference (§7): the original id is immutable; the live FK is nulled on delete,
    # but the historical evidence (original id + content hash + deletion time) is preserved.
    created_anomaly_original_id: Mapped[int | None] = mapped_column(BigInteger)
    created_anomaly_hash: Mapped[str | None] = mapped_column(Text)
    created_anomaly_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_anomaly_live_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("price_anomaly.id")
    )
    status: Mapped[str] = mapped_column(
        enum_col(*REMEDIATION_CHANGE_STATUS, name="history_remediation_change_status"),
        nullable=False, server_default="planned",
    )
    error_code: Mapped[str | None] = mapped_column(Text)  # sanitized code, never a raw message
