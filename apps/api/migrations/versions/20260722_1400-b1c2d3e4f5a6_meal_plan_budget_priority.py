"""meal plan budget priority

Revision ID: b1c2d3e4f5a6
Revises: 405e472c04b3
Create Date: 2026-07-22 14:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: str | None = '405e472c04b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'meal_plan',
        sa.Column(
            'budget_priority',
            sa.Text(),
            server_default='waste',
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('meal_plan', 'budget_priority')
