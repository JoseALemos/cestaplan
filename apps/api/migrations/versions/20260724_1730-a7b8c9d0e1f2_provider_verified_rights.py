"""provider verified rights + explicit rights scope

Adds the rights/authorization columns to ``provider_activation``: a verified authorization
status, the licence basis and public display names, an explicit JSONB ``rights_scope``,
public attribution text, validity window, and INTERNAL-ONLY evidence/notes columns (never
serialised to non-admin surfaces). Schema only — no data is populated here; the
``bootstrap_source_rights`` tool records the actual authorized state per source.

Revision ID: a7b8c9d0e1f2
Revises: cae826099316
Create Date: 2026-07-24 17:30:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7b8c9d0e1f2'
down_revision: str | None = 'cae826099316'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'provider_activation',
        sa.Column('authorization_status', sa.Text(), server_default='unknown', nullable=False),
    )
    op.add_column('provider_activation', sa.Column('license_basis', sa.Text(), nullable=True))
    op.add_column(
        'provider_activation', sa.Column('license_display_name', sa.Text(), nullable=True)
    )
    op.add_column(
        'provider_activation', sa.Column('rights_display_name', sa.Text(), nullable=True)
    )
    op.add_column(
        'provider_activation',
        sa.Column('rights_scope', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column('authorization_verified_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'provider_activation',
        sa.Column('authorization_verified_by', sa.BigInteger(), nullable=True),
    )
    op.add_column(
        'provider_activation', sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'provider_activation', sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'provider_activation', sa.Column('attribution_text_public', sa.Text(), nullable=True)
    )
    op.add_column(
        'provider_activation', sa.Column('internal_evidence_reference', sa.Text(), nullable=True)
    )
    op.add_column(
        'provider_activation', sa.Column('legal_notes_internal', sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        'fk_provider_activation_authorization_verified_by_user',
        'provider_activation',
        'user',
        ['authorization_verified_by'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_provider_activation_authorization_verified_by_user',
        'provider_activation',
        type_='foreignkey',
    )
    op.drop_column('provider_activation', 'legal_notes_internal')
    op.drop_column('provider_activation', 'internal_evidence_reference')
    op.drop_column('provider_activation', 'attribution_text_public')
    op.drop_column('provider_activation', 'valid_until')
    op.drop_column('provider_activation', 'valid_from')
    op.drop_column('provider_activation', 'authorization_verified_by')
    op.drop_column('provider_activation', 'authorization_verified_at')
    op.drop_column('provider_activation', 'rights_scope')
    op.drop_column('provider_activation', 'rights_display_name')
    op.drop_column('provider_activation', 'license_display_name')
    op.drop_column('provider_activation', 'license_basis')
    op.drop_column('provider_activation', 'authorization_status')
