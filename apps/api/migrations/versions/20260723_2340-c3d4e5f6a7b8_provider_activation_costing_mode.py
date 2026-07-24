"""provider activation costing-mode coverage

Adds per-provider aggregate coverage of the per-product costing mode (spec audit): fraction of
fixed packages, of genuine variable-weight/volume items, and of products that could NOT be
resolved for costing (a bare reference unit_price no longer counts as costable).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-23 23:40:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: str | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    'package_coverage',
    'variable_weight_coverage',
    'unresolved_costing_coverage',
)


def upgrade() -> None:
    for col in _COLS:
        op.add_column('provider_activation', sa.Column(col, sa.Numeric(5, 4), nullable=True))


def downgrade() -> None:
    for col in reversed(_COLS):
        op.drop_column('provider_activation', col)
