"""recipe + recipe-ingredient provenance / verification metadata

Additive, nullable columns only — no recipe content or quantity is altered. Records where a recipe
came from and whether an ingredient quantity is original, AI-estimated or human-verified, so an
estimated quantity is never presented as verified. Data backfill is done idempotently by the
``backfill_recipe_provenance`` tool (dry-run first), not here.

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-07-25 12:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: str | None = 'a7b8c9d0e1f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('recipe', sa.Column('source_dataset', sa.Text(), nullable=True))
    op.add_column('recipe', sa.Column('source_reference', sa.Text(), nullable=True))
    op.add_column('recipe', sa.Column('source_license', sa.Text(), nullable=True))
    op.add_column('recipe', sa.Column('imported_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('recipe', sa.Column('verification_status', sa.Text(), nullable=True))
    op.add_column('recipe', sa.Column('estimation_model', sa.Text(), nullable=True))
    op.add_column('recipe', sa.Column('estimation_prompt_version', sa.Text(), nullable=True))
    op.add_column('recipe_ingredient', sa.Column('quantity_source', sa.Text(), nullable=True))
    op.add_column(
        'recipe_ingredient', sa.Column('quantity_confidence', sa.Numeric(5, 4), nullable=True)
    )
    op.add_column('recipe_ingredient', sa.Column('verification_status', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('recipe_ingredient', 'verification_status')
    op.drop_column('recipe_ingredient', 'quantity_confidence')
    op.drop_column('recipe_ingredient', 'quantity_source')
    op.drop_column('recipe', 'estimation_prompt_version')
    op.drop_column('recipe', 'estimation_model')
    op.drop_column('recipe', 'verification_status')
    op.drop_column('recipe', 'imported_at')
    op.drop_column('recipe', 'source_license')
    op.drop_column('recipe', 'source_reference')
    op.drop_column('recipe', 'source_dataset')
