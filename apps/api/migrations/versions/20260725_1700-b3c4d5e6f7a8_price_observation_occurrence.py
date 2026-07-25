"""price_observation_occurrence: Layer B provenance of the two-layer price-fact model (spec §1)

One PriceObservation (Layer A) is a unique economic fact; each occasion a provider/crawl/parser
confirmed it is a row here. Creates the table only — no data is moved and NOTHING is deleted; the
idempotent backfill of one occurrence per historical observation is a separate, reviewable step.

Revision ID: b3c4d5e6f7a8
Revises: f7e8d9c0b1a2
Create Date: 2026-07-25 17:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "f7e8d9c0b1a2"
branch_labels = None
depends_on = None

_TABLE = "price_observation_occurrence"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("price_observation_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_code", sa.Text(), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("crawl_run_id", sa.BigInteger(), nullable=True),
        sa.Column("raw_capture_id", sa.BigInteger(), nullable=True),
        sa.Column("connector_version", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.Text(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence_score", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "verification_status",
            sa.Enum(
                "unverified",
                "machine_verified",
                "human_verified",
                "disputed",
                name="price_obs_occurrence_verification_status",
                native_enum=False,
            ),
            server_default="unverified",
            nullable=False,
        ),
        sa.Column("evidence_fingerprint", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["price_observation_id"], ["price_observation.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["data_source.id"]),
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_run.id"]),
        sa.ForeignKeyConstraint(["raw_capture_id"], ["raw_capture.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_price_observation_occurrence_public_id"),
        _TABLE,
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_price_obs_occurrence_observation", _TABLE, ["price_observation_id"], unique=False
    )
    op.create_index(
        "ix_price_obs_occurrence_identity",
        _TABLE,
        [
            "price_observation_id",
            "provider_code",
            "source_id",
            "crawl_run_id",
            "raw_capture_id",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_price_obs_occurrence_identity", table_name=_TABLE)
    op.drop_index("ix_price_obs_occurrence_observation", table_name=_TABLE)
    op.drop_index(op.f("ix_price_observation_occurrence_public_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
