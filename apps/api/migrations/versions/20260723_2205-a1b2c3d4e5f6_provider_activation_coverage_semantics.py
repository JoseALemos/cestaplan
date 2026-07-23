"""provider activation coverage semantics

Separate DECLARED intent from OBSERVED coverage on ``provider_activation``:
rename ``catalog_scope`` -> ``intended_catalog_scope`` and add the measured-coverage
columns (observed scope, price/package/geographic coverage ratios, costing/production
eligibility). A sample-only capture must never read as a full catalogue.

Revision ID: a1b2c3d4e5f6
Revises: 43b334ddd564
Create Date: 2026-07-23 22:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '43b334ddd564'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        'provider_activation',
        'catalog_scope',
        new_column_name='intended_catalog_scope',
    )
    op.add_column(
        'provider_activation',
        sa.Column(
            'observed_catalog_scope',
            sa.Text(),
            server_default='unknown',
            nullable=False,
        ),
    )
    op.add_column(
        'provider_activation',
        sa.Column('price_coverage', sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column('package_quantity_coverage', sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column('package_unit_coverage', sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column('geographic_scope_coverage', sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column(
            'costing_eligibility',
            sa.Text(),
            server_default='unknown',
            nullable=False,
        ),
    )
    op.add_column(
        'provider_activation',
        sa.Column(
            'production_eligibility',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column('provider_activation', 'production_eligibility')
    op.drop_column('provider_activation', 'costing_eligibility')
    op.drop_column('provider_activation', 'geographic_scope_coverage')
    op.drop_column('provider_activation', 'package_unit_coverage')
    op.drop_column('provider_activation', 'package_quantity_coverage')
    op.drop_column('provider_activation', 'price_coverage')
    op.drop_column('provider_activation', 'observed_catalog_scope')
    op.alter_column(
        'provider_activation',
        'intended_catalog_scope',
        new_column_name='catalog_scope',
    )
