"""competing candidate semantics + restore wrongly-superseded rows

Fixes the deduplication that wrongly consolidated COMPETING candidates (same product, different
ingredient) as duplicates. Adds relation/conflict columns, tightens candidate uniqueness to
include mapping_version, enforces at most one approved+active mapping per product/provider, and
restores the wrongly-superseded competing candidates. Data steps are idempotent.

Revision ID: d1e2f3a4b5c6
Revises: 8026415c5fd5
Create Date: 2026-07-24 10:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "8026415c5fd5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "provider_ingredient_mapping"


def upgrade() -> None:
    op.add_column(_T, sa.Column("relation_status", sa.Text(), server_default="independent", nullable=False))
    op.add_column(_T, sa.Column("conflict_group_id", sa.Text(), nullable=True))
    op.add_column(_T, sa.Column("conflict_reason", sa.Text(), nullable=True))
    op.add_column(_T, sa.Column("resolved_by_mapping_id", sa.BigInteger(), nullable=True))
    op.add_column(_T, sa.Column("conflict_resolved_at", sa.DateTime(timezone=True), nullable=True))

    # Candidate uniqueness now includes mapping_version.
    op.drop_index("ux_provider_ing_map", table_name=_T)
    op.create_index(
        "ux_provider_ing_map", _T,
        ["provider_code", "ingredient_id", "external_product_id", "mapping_version"], unique=True,
    )

    # --- restore wrongly-superseded COMPETING candidates (idempotent) ----------------------- #
    # Only rows my dedup marked ("product better mapped ...") that are NOT a manual reject and NOT
    # incompatible/rejected. After restore, superseded_reason is NULL so re-running is a no-op.
    op.execute(
        f"""
        UPDATE {_T}
        SET superseded_at = NULL, superseded_reason = NULL, active = false,
            relation_status = 'competing'
        WHERE superseded_at IS NOT NULL
          AND superseded_reason LIKE 'product better mapped%'
          AND mapping_status NOT IN ('rejected', 'incompatible', 'manually_approved')
        """
    )
    # Tag conflict groups: any product claimed by >1 ingredient shares a stable group id.
    op.execute(
        f"""
        UPDATE {_T} m
        SET conflict_group_id = m.provider_code || ':' || m.external_product_id,
            relation_status = CASE
                WHEN m.mapping_status IN ('auto_approved','manually_approved') AND m.active
                    THEN 'conflict_resolved' ELSE 'competing' END
        WHERE (m.provider_code, m.external_product_id) IN (
            SELECT provider_code, external_product_id FROM {_T}
            GROUP BY provider_code, external_product_id HAVING count(DISTINCT ingredient_id) > 1
        )
        """
    )

    # At most one approved+active mapping per product/provider (partial unique index).
    op.create_index(
        "ux_provider_approved_product", _T, ["provider_code", "external_product_id"], unique=True,
        postgresql_where=sa.text("active AND mapping_status IN ('auto_approved','manually_approved')"),
    )


def downgrade() -> None:
    op.drop_index("ux_provider_approved_product", table_name=_T)
    op.drop_index("ux_provider_ing_map", table_name=_T)
    op.create_index(
        "ux_provider_ing_map", _T,
        ["provider_code", "ingredient_id", "external_product_id"], unique=True,
    )
    for col in (
        "conflict_resolved_at", "resolved_by_mapping_id", "conflict_reason",
        "conflict_group_id", "relation_status",
    ):
        op.drop_column(_T, col)
