"""household_habitual_weekly_spend: gasto semanal habitual declarado por el hogar

Aditivo y nullable: referencia opcional para estimar el ahorro del plan frente a lo que el
hogar suele gastar. Un hogar que no lo declara simplemente no ve la estimación. Nada se borra.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-19 16:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "household", sa.Column("habitual_weekly_spend", sa.Numeric(10, 2), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("household", "habitual_weekly_spend")
