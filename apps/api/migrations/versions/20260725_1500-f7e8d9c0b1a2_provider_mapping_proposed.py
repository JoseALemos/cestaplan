"""provider_ingredient_mapping: record the machine proposal for review-only discovery

Revision ID: c3d4e5f6a7b8
Revises: d4e5f6a7b8c9
Create Date: 2026-07-25 15:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7e8d9c0b1a2"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_TABLE = "provider_ingredient_mapping"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("proposed_mapping_status", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("proposed_confidence", sa.Numeric(5, 4), nullable=True))
    op.add_column(_TABLE, sa.Column("proposed_method", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "proposed_method")
    op.drop_column(_TABLE, "proposed_confidence")
    op.drop_column(_TABLE, "proposed_mapping_status")
