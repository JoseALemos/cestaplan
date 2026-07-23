"""provider activation extra coverage metrics

Adds the remaining §12 per-provider coverage metrics to ``provider_activation``:
identifier / barcode / observed_at coverage and the fraction of individually costable products.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-23 22:50:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLS = (
    'identifier_coverage',
    'barcode_coverage',
    'observed_at_coverage',
    'costing_eligible_product_coverage',
)


def upgrade() -> None:
    for col in _COLS:
        op.add_column('provider_activation', sa.Column(col, sa.Numeric(5, 4), nullable=True))


def downgrade() -> None:
    for col in reversed(_COLS):
        op.drop_column('provider_activation', col)
