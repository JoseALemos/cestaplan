"""ingredient identity consolidation (merge-to-slug + alias table)

Recipe costing matches recipe <-> product by ``ingredient_id``, never by name. The
``ingredient`` table had fractured identity: the same real ingredient existed both as a
canonical slug row (``aceite_oliva``) and as accented / plural / spaced variant rows
(``aceite de oliva``, ``azúcar``, ``aceitunas``). Recipes point at the variants while the
mappings and ``_SPECS`` point at the slugs, so those recipes never cost.

This migration folds every variant into its surviving slug row and records each variant
name as an alias, deterministically. The merge plan is computed by
``services.ingredient_consolidation`` from the *live* rows (nothing is hardcoded), so a DB
that has no duplicates (e.g. a fresh seed) is a clean no-op. Every fold is written to
``ingredient_merge_audit`` so ``downgrade`` can reconstitute the deleted rows and re-point
the foreign keys.

Revision ID: c7f4a1b9e2d3
Revises: 35d510ebc887
Create Date: 2026-08-08 10:00:00.000000

"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cestaplan_api.services.ingredient_consolidation import build_consolidation_plan

# revision identifiers, used by Alembic.
revision: str = 'c7f4a1b9e2d3'
down_revision: str | None = '35d510ebc887'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #
def upgrade() -> None:
    op.create_table(
        'ingredient_alias',
        sa.Column('alias_text', sa.Text(), nullable=False),
        sa.Column('ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('public_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ingredient_id'], ['ingredient.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingredient_alias_public_id'), 'ingredient_alias',
                    ['public_id'], unique=True)
    op.create_index('ux_ingredient_alias_text', 'ingredient_alias', ['alias_text'], unique=True)

    op.create_table(
        'ingredient_merge_audit',
        # No FK on old_ingredient_id: that row is deleted by this migration, so an FK would
        # be violated. new_ingredient_id references the surviving row (which persists).
        sa.Column('old_ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('new_ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('old_canonical_name', sa.Text(), nullable=False),
        sa.Column('merged_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('public_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['new_ingredient_id'], ['ingredient.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingredient_merge_audit_public_id'), 'ingredient_merge_audit',
                    ['public_id'], unique=True)
    op.create_index('ix_ingredient_merge_audit_old', 'ingredient_merge_audit',
                    ['old_ingredient_id'], unique=False)
    op.create_index('ix_ingredient_merge_audit_new', 'ingredient_merge_audit',
                    ['new_ingredient_id'], unique=False)

    _apply_consolidation(op.get_bind())


def _load_active_mapping_ids(bind: sa.engine.Connection) -> set[int]:
    """Ingredient ids that are the target of an active product or provider mapping."""
    ids: set[int] = set()
    for (ingredient_id,) in bind.execute(sa.text(
        "SELECT DISTINCT ingredient_id FROM ingredient_product_mapping WHERE is_active = true"
    )):
        ids.add(ingredient_id)
    for (ingredient_id,) in bind.execute(sa.text(
        "SELECT DISTINCT ingredient_id FROM provider_ingredient_mapping WHERE active = true"
    )):
        ids.add(ingredient_id)
    return ids


def _apply_consolidation(bind: sa.engine.Connection) -> None:
    ingredients = [
        (row[0], row[1])
        for row in bind.execute(sa.text("SELECT id, canonical_name FROM ingredient"))
    ]
    plan = build_consolidation_plan(
        ingredients, active_mapping_ingredient_ids=_load_active_mapping_ids(bind)
    )

    # Record aliases (survivors persist, so this is safe before any delete). Idempotent.
    for alias in plan.aliases:
        bind.execute(
            sa.text(
                "INSERT INTO ingredient_alias (alias_text, ingredient_id, public_id) "
                "VALUES (:alias_text, :ingredient_id, :public_id) "
                "ON CONFLICT (alias_text) DO NOTHING"
            ),
            {
                "alias_text": alias.alias_text,
                "ingredient_id": alias.ingredient_id,
                "public_id": uuid.uuid4(),
            },
        )

    for merge in plan.merges:
        params = {"old": merge.old_id, "new": merge.new_id, "newname": merge.new_canonical_name}

        bind.execute(
            sa.text(
                "INSERT INTO ingredient_merge_audit "
                "(old_ingredient_id, new_ingredient_id, old_canonical_name, public_id) "
                "VALUES (:old, :new, :oldname, :public_id)"
            ),
            {
                "old": merge.old_id,
                "new": merge.new_id,
                "oldname": merge.old_canonical_name,
                "public_id": uuid.uuid4(),
            },
        )

        # Re-point recipe references onto the survivor, aligning the denormalized name.
        bind.execute(
            sa.text(
                "UPDATE recipe_ingredient SET ingredient_id = :new, canonical_name = :newname "
                "WHERE ingredient_id = :old"
            ),
            params,
        )
        # Household-side references (no unique on ingredient_id -> a plain re-point).
        bind.execute(
            sa.text("UPDATE pantry_item SET ingredient_id = :new WHERE ingredient_id = :old"),
            params,
        )
        bind.execute(
            sa.text(
                "UPDATE grocery_list_item SET ingredient_id = :new WHERE ingredient_id = :old"
            ),
            params,
        )

        # ingredient_product_mapping: unique on (ingredient_id, product_id). Drop the old row
        # when the survivor already owns that product (the survivor mapping wins); otherwise
        # re-point it.
        bind.execute(
            sa.text(
                "DELETE FROM ingredient_product_mapping o "
                "WHERE o.ingredient_id = :old AND EXISTS ("
                "  SELECT 1 FROM ingredient_product_mapping n "
                "  WHERE n.ingredient_id = :new AND n.product_id = o.product_id)"
            ),
            params,
        )
        bind.execute(
            sa.text(
                "UPDATE ingredient_product_mapping SET ingredient_id = :new "
                "WHERE ingredient_id = :old"
            ),
            params,
        )

        # provider_ingredient_mapping: full unique on
        # (provider_code, ingredient_id, external_product_id, mapping_version) + a partial
        # unique on (provider_code, external_product_id) for active approved rows.
        # 1) drop rows that would collide on the full key,
        bind.execute(
            sa.text(
                "DELETE FROM provider_ingredient_mapping o "
                "WHERE o.ingredient_id = :old AND EXISTS ("
                "  SELECT 1 FROM provider_ingredient_mapping n "
                "  WHERE n.ingredient_id = :new AND n.provider_code = o.provider_code "
                "    AND n.external_product_id = o.external_product_id "
                "    AND n.mapping_version = o.mapping_version)"
            ),
            params,
        )
        # 2) deactivate a surviving-but-still-colliding approved duplicate (partial index),
        bind.execute(
            sa.text(
                "UPDATE provider_ingredient_mapping o SET active = false "
                "WHERE o.ingredient_id = :old AND o.active = true "
                "  AND o.mapping_status IN ('auto_approved','manually_approved') "
                "  AND EXISTS ("
                "  SELECT 1 FROM provider_ingredient_mapping n "
                "  WHERE n.ingredient_id = :new AND n.active = true "
                "    AND n.mapping_status IN ('auto_approved','manually_approved') "
                "    AND n.provider_code = o.provider_code "
                "    AND n.external_product_id = o.external_product_id)"
            ),
            params,
        )
        # 3) re-point the remainder (now free of unique conflicts).
        bind.execute(
            sa.text(
                "UPDATE provider_ingredient_mapping SET ingredient_id = :new "
                "WHERE ingredient_id = :old"
            ),
            params,
        )

        # The orphan variant row now has no inbound FKs -> delete it.
        bind.execute(
            sa.text("DELETE FROM ingredient WHERE id = :old"), {"old": merge.old_id}
        )


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    _revert_consolidation(op.get_bind())

    op.drop_index('ix_ingredient_merge_audit_new', table_name='ingredient_merge_audit')
    op.drop_index('ix_ingredient_merge_audit_old', table_name='ingredient_merge_audit')
    op.drop_index(op.f('ix_ingredient_merge_audit_public_id'),
                  table_name='ingredient_merge_audit')
    op.drop_table('ingredient_merge_audit')

    op.drop_index('ux_ingredient_alias_text', table_name='ingredient_alias')
    op.drop_index(op.f('ix_ingredient_alias_public_id'), table_name='ingredient_alias')
    op.drop_table('ingredient_alias')


def _revert_consolidation(bind: sa.engine.Connection) -> None:
    """Reconstitute the merged-away rows from the audit and re-point the FKs back.

    Reversibility notes: the audit records ingredient identity, not per-recipe provenance,
    so ``display_name`` is restored as the canonical name (its original free-text label is
    not retained). Re-pointing moves every FK currently on the survivor that came from a
    fold back to the reconstituted variant; this is exact for the real consolidation shape
    (each survivor received a single fold and carried no recipe rows of its own) and always
    preserves integrity (row counts, quantities, resolvable FKs). Product/provider mappings
    are not moved back — deleted duplicates are irreversible and re-pointed ones stay valid
    on the survivor.
    """
    audits = bind.execute(sa.text(
        "SELECT old_ingredient_id, new_ingredient_id, old_canonical_name "
        "FROM ingredient_merge_audit ORDER BY id"
    )).all()

    for old_id, new_id, old_name in audits:
        params = {"old": old_id, "new": new_id, "oldname": old_name}
        # Recreate the deleted variant row (explicit id; identity is GENERATED BY DEFAULT).
        bind.execute(
            sa.text(
                "INSERT INTO ingredient (id, canonical_name, display_name, is_synthetic, "
                "public_id) VALUES (:old, :oldname, :oldname, false, :public_id) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"old": old_id, "oldname": old_name, "public_id": uuid.uuid4()},
        )
        # Move recipe references (that were folded into the survivor) back to the variant.
        bind.execute(
            sa.text(
                "UPDATE recipe_ingredient SET ingredient_id = :old, canonical_name = :oldname "
                "WHERE ingredient_id = :new"
            ),
            params,
        )
        bind.execute(
            sa.text("UPDATE pantry_item SET ingredient_id = :old WHERE ingredient_id = :new"),
            params,
        )
        bind.execute(
            sa.text(
                "UPDATE grocery_list_item SET ingredient_id = :old WHERE ingredient_id = :new"
            ),
            params,
        )
