"""ingredient identity consolidation (merge-to-slug + alias table)

Recipe costing matches recipe <-> product by ``ingredient_id``, never by name. The
``ingredient`` table had fractured identity: the same real ingredient existed both as a
canonical slug row (``aceite_oliva``) and as accented / plural / spaced variant rows
(``aceite de oliva``, ``azúcar``, ``aceitunas``). Recipes point at the variants while the
mappings and ``_SPECS`` point at the slugs, so those recipes never cost.

This migration folds every variant into its surviving slug row and records each variant name
as an alias, deterministically. The merge plan is computed by
``services.ingredient_consolidation`` from the *live* rows (nothing is hardcoded), so a DB
that has no duplicates (e.g. a fresh seed) is a clean no-op.

Reversibility is exact. For every fold we persist:

* the full ``to_jsonb`` snapshot of the deleted variant row (``ingredient_merge_audit``),
* one row per re-pointed foreign key, keyed by ``(source_table, source_row_id)``
  (``ingredient_merge_fk_relink``), so downgrade reverts each reference by *row id* rather
  than by ``WHERE ingredient_id = survivor`` — correct for 3+ member groups and for survivors
  that already had their own references,
* the full snapshot of any row physically deleted — mapping rows dropped on a unique-index
  collision, and ``recipe_ingredient`` lines removed by dedup — so they are re-inserted verbatim
  (``ingredient_merge_deleted_row``).

The forward pass does **not** rewrite ``recipe_ingredient.canonical_name`` (a UI-visible label);
it changes only ``ingredient_id`` — costing joins on the id, so prices are unaffected.

Revision ID: c7f4a1b9e2d3
Revises: 35d510ebc887
Create Date: 2026-08-08 10:00:00.000000

"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from cestaplan_api.services.ingredient_consolidation import build_consolidation_plan

# revision identifiers, used by Alembic.
revision: str = 'c7f4a1b9e2d3'
down_revision: str | None = '35d510ebc887'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

# Tables whose ingredient_id FK is re-pointed by a plain relink (no unique index on it).
_SIMPLE_FK_TABLES = ("recipe_ingredient", "pantry_item", "grocery_list_item")
# Tables re-inserted verbatim on downgrade. Whitelist guards the dynamic SQL below.
_RESTORABLE_TABLES = frozenset(
    (*_SIMPLE_FK_TABLES, "ingredient_product_mapping", "provider_ingredient_mapping")
)


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
        # No FK on old_ingredient_id: that row is deleted by this migration.
        sa.Column('old_ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('new_ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('old_canonical_name', sa.Text(), nullable=False),
        sa.Column('old_ingredient_snapshot', JSONB(), nullable=False),
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

    op.create_table(
        'ingredient_merge_fk_relink',
        sa.Column('merge_audit_id', sa.BigInteger(), nullable=False),
        sa.Column('source_table', sa.Text(), nullable=False),
        sa.Column('source_row_id', sa.BigInteger(), nullable=False),
        sa.Column('old_ingredient_id', sa.BigInteger(), nullable=False),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('public_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['merge_audit_id'], ['ingredient_merge_audit.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingredient_merge_fk_relink_public_id'),
                    'ingredient_merge_fk_relink', ['public_id'], unique=True)
    op.create_index('ix_ingredient_merge_relink_audit', 'ingredient_merge_fk_relink',
                    ['merge_audit_id'], unique=False)

    op.create_table(
        'ingredient_merge_deleted_row',
        sa.Column('merge_audit_id', sa.BigInteger(), nullable=False),
        sa.Column('source_table', sa.Text(), nullable=False),
        sa.Column('row_data', JSONB(), nullable=False),
        sa.Column('merged_into_row_id', sa.BigInteger(), nullable=True),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('public_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['merge_audit_id'], ['ingredient_merge_audit.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ingredient_merge_deleted_row_public_id'),
                    'ingredient_merge_deleted_row', ['public_id'], unique=True)
    op.create_index('ix_ingredient_merge_deleted_audit', 'ingredient_merge_deleted_row',
                    ['merge_audit_id'], unique=False)

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

    # (6) Log the computed plan for human review in the deploy log, before touching data.
    if plan.merges:
        log.info("ingredient consolidation: applying %d fold(s)", len(plan.merges))
        for merge in plan.merges:
            log.info(
                "  fold %r (id=%s) -> %r (id=%s)",
                merge.old_canonical_name, merge.old_id,
                merge.new_canonical_name, merge.new_id,
            )
    else:
        log.info("ingredient consolidation: no duplicates detected; no folds applied")

    # Record aliases (survivors persist, so this is safe before any delete). Idempotent.
    for alias in plan.aliases:
        bind.execute(
            sa.text(
                "INSERT INTO ingredient_alias (alias_text, ingredient_id, public_id) "
                "VALUES (:alias_text, :ingredient_id, :public_id) "
                "ON CONFLICT (alias_text) DO NOTHING"
            ),
            {"alias_text": alias.alias_text, "ingredient_id": alias.ingredient_id,
             "public_id": uuid.uuid4()},
        )

    audit_id_by_new: dict[int, int] = {}
    for merge in plan.merges:
        snapshot = bind.execute(
            sa.text("SELECT to_jsonb(t.*) FROM ingredient t WHERE id = :old"),
            {"old": merge.old_id},
        ).scalar_one()
        audit_id = bind.execute(
            sa.text(
                "INSERT INTO ingredient_merge_audit "
                "(old_ingredient_id, new_ingredient_id, old_canonical_name, "
                " old_ingredient_snapshot, public_id) "
                "VALUES (:old, :new, :oldname, CAST(:snap AS jsonb), :public_id) RETURNING id"
            ),
            {"old": merge.old_id, "new": merge.new_id, "oldname": merge.old_canonical_name,
             "snap": _as_json_text(snapshot), "public_id": uuid.uuid4()},
        ).scalar_one()
        audit_id_by_new.setdefault(merge.new_id, audit_id)

        # (3) Re-point simple FKs by row id (canonical_name left untouched on recipe lines).
        for table in _SIMPLE_FK_TABLES:
            _relink_simple(bind, audit_id, table, merge.old_id, merge.new_id)

        # Mapping tables: snapshot+drop unique-index collisions, then relink the rest.
        _reconcile_mapping(
            bind, audit_id, "ingredient_product_mapping", merge.old_id, merge.new_id,
            collision_exists=(
                "EXISTS (SELECT 1 FROM ingredient_product_mapping n "
                "WHERE n.ingredient_id = :new AND n.product_id = o.product_id)"
            ),
        )
        # (5) provider mappings only collide on the FULL unique key. The partial approved index
        # ux_provider_approved_product(provider_code, external_product_id) is globally unique
        # across ALL ingredient_ids, so re-pointing can never introduce a second active-approved
        # row for a (provider_code, external_product_id) — no deactivation step is needed.
        _reconcile_mapping(
            bind, audit_id, "provider_ingredient_mapping", merge.old_id, merge.new_id,
            collision_exists=(
                "EXISTS (SELECT 1 FROM provider_ingredient_mapping n "
                "WHERE n.ingredient_id = :new AND n.provider_code = o.provider_code "
                "  AND n.external_product_id = o.external_product_id "
                "  AND n.mapping_version = o.mapping_version)"
            ),
        )

        bind.execute(sa.text("DELETE FROM ingredient WHERE id = :old"), {"old": merge.old_id})

    # (4) A fold can make a recipe cite the same ingredient twice; merge those lines.
    _dedup_recipe_ingredients(bind, audit_id_by_new)


def _relink_simple(
    bind: sa.engine.Connection, audit_id: int, table: str, old_id: int, new_id: int
) -> None:
    row_ids = [r[0] for r in bind.execute(
        sa.text(f"SELECT id FROM {table} WHERE ingredient_id = :old"), {"old": old_id}
    )]
    for row_id in row_ids:
        _record_relink(bind, audit_id, table, row_id, old_id)
    if row_ids:
        bind.execute(
            sa.text(f"UPDATE {table} SET ingredient_id = :new WHERE ingredient_id = :old"),
            {"old": old_id, "new": new_id},
        )


def _reconcile_mapping(
    bind: sa.engine.Connection, audit_id: int, table: str, old_id: int, new_id: int,
    *, collision_exists: str,
) -> None:
    # Snapshot + delete rows that would violate a unique index if re-pointed onto the survivor.
    colliding = bind.execute(
        sa.text(
            f"SELECT o.id, to_jsonb(o.*) FROM {table} o "
            f"WHERE o.ingredient_id = :old AND {collision_exists}"
        ),
        {"old": old_id, "new": new_id},
    ).all()
    for row_id, row_data in colliding:
        _record_deleted_row(bind, audit_id, table, row_data, None)
        bind.execute(sa.text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})

    # The rest carry no conflict -> relink by row id and re-point.
    row_ids = [r[0] for r in bind.execute(
        sa.text(f"SELECT id FROM {table} WHERE ingredient_id = :old"), {"old": old_id}
    )]
    for row_id in row_ids:
        _record_relink(bind, audit_id, table, row_id, old_id)
    if row_ids:
        bind.execute(
            sa.text(f"UPDATE {table} SET ingredient_id = :new WHERE ingredient_id = :old"),
            {"old": old_id, "new": new_id},
        )


def _dedup_recipe_ingredients(
    bind: sa.engine.Connection, audit_id_by_new: dict[int, int]
) -> None:
    if not audit_id_by_new:
        return
    survivor_ids = list(audit_id_by_new)
    repointed = {
        r[0]
        for r in bind.execute(sa.text(
            "SELECT source_row_id FROM ingredient_merge_fk_relink "
            "WHERE source_table = 'recipe_ingredient'"
        ))
    }
    groups = bind.execute(
        sa.text(
            "SELECT recipe_id, ingredient_id, array_agg(id ORDER BY id) AS ids "
            "FROM recipe_ingredient WHERE ingredient_id IN :sids "
            "GROUP BY recipe_id, ingredient_id HAVING COUNT(*) > 1"
        ).bindparams(sa.bindparam("sids", expanding=True)),
        {"sids": survivor_ids},
    ).all()

    for _recipe_id, ingredient_id, ids in groups:
        # Only merge duplication that THIS migration created (a fold touched the group).
        if not (set(ids) & repointed):
            continue
        kept = ids[0]
        audit_id = audit_id_by_new[ingredient_id]
        for dup_id in ids[1:]:
            row_data, quantity = bind.execute(
                sa.text("SELECT to_jsonb(t.*), t.quantity FROM recipe_ingredient t "
                        "WHERE t.id = :id"),
                {"id": dup_id},
            ).one()
            _record_deleted_row(bind, audit_id, "recipe_ingredient", row_data, kept)
            bind.execute(
                sa.text("UPDATE recipe_ingredient SET quantity = quantity + :q WHERE id = :kept"),
                {"q": quantity, "kept": kept},
            )
            bind.execute(
                sa.text("DELETE FROM recipe_ingredient WHERE id = :id"), {"id": dup_id}
            )


def _record_relink(
    bind: sa.engine.Connection, audit_id: int, table: str, row_id: int, old_id: int
) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO ingredient_merge_fk_relink "
            "(merge_audit_id, source_table, source_row_id, old_ingredient_id, public_id) "
            "VALUES (:aid, :tbl, :rid, :old, :public_id)"
        ),
        {"aid": audit_id, "tbl": table, "rid": row_id, "old": old_id, "public_id": uuid.uuid4()},
    )


def _record_deleted_row(
    bind: sa.engine.Connection, audit_id: int, table: str, row_data, merged_into: int | None
) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO ingredient_merge_deleted_row "
            "(merge_audit_id, source_table, row_data, merged_into_row_id, public_id) "
            "VALUES (:aid, :tbl, CAST(:data AS jsonb), :merged, :public_id)"
        ),
        {"aid": audit_id, "tbl": table, "data": _as_json_text(row_data), "merged": merged_into,
         "public_id": uuid.uuid4()},
    )


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def downgrade() -> None:
    _revert_consolidation(op.get_bind())

    op.drop_index('ix_ingredient_merge_deleted_audit', table_name='ingredient_merge_deleted_row')
    op.drop_index(op.f('ix_ingredient_merge_deleted_row_public_id'),
                  table_name='ingredient_merge_deleted_row')
    op.drop_table('ingredient_merge_deleted_row')

    op.drop_index('ix_ingredient_merge_relink_audit', table_name='ingredient_merge_fk_relink')
    op.drop_index(op.f('ix_ingredient_merge_fk_relink_public_id'),
                  table_name='ingredient_merge_fk_relink')
    op.drop_table('ingredient_merge_fk_relink')

    op.drop_index('ix_ingredient_merge_audit_new', table_name='ingredient_merge_audit')
    op.drop_index('ix_ingredient_merge_audit_old', table_name='ingredient_merge_audit')
    op.drop_index(op.f('ix_ingredient_merge_audit_public_id'),
                  table_name='ingredient_merge_audit')
    op.drop_table('ingredient_merge_audit')

    op.drop_index('ux_ingredient_alias_text', table_name='ingredient_alias')
    op.drop_index(op.f('ix_ingredient_alias_public_id'), table_name='ingredient_alias')
    op.drop_table('ingredient_alias')


def _revert_consolidation(bind: sa.engine.Connection) -> None:
    """Reconstitute every merged-away row and re-point each FK back to its exact origin.

    Order matters: recreate the variant ``ingredient`` rows first (so FKs can reference them),
    re-insert deleted mapping rows, undo the recipe-line dedup (restore quantities + re-insert
    the removed lines), and finally revert every recorded relink by row id.
    """
    # 1. Recreate the deleted variant rows verbatim from their JSON snapshot.
    for (snapshot,) in bind.execute(sa.text(
        "SELECT old_ingredient_snapshot FROM ingredient_merge_audit ORDER BY id"
    )):
        bind.execute(
            sa.text(
                "INSERT INTO ingredient SELECT * FROM "
                "jsonb_populate_record(NULL::ingredient, CAST(:data AS jsonb)) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"data": _as_json_text(snapshot)},
        )

    # 2. Re-insert deleted mapping rows (they reference the now-recreated variant ids).
    for source_table, row_data in bind.execute(sa.text(
        "SELECT source_table, row_data FROM ingredient_merge_deleted_row "
        "WHERE source_table <> 'recipe_ingredient' ORDER BY id"
    )):
        _restore_deleted_row(bind, source_table, row_data)

    # 3. Undo recipe-line dedup. First revert the absorbed quantity on each kept line — done
    # entirely in SQL from the stored JSONB (exact numeric, never a Python float round-trip) —
    # then re-insert every removed line verbatim.
    bind.execute(sa.text(
        "UPDATE recipe_ingredient r SET quantity = r.quantity - sub.q FROM ("
        "  SELECT merged_into_row_id AS kept, "
        "         SUM((row_data->>'quantity')::numeric) AS q "
        "  FROM ingredient_merge_deleted_row "
        "  WHERE source_table = 'recipe_ingredient' AND merged_into_row_id IS NOT NULL "
        "  GROUP BY merged_into_row_id"
        ") sub WHERE r.id = sub.kept"
    ))
    for (row_data,) in bind.execute(sa.text(
        "SELECT row_data FROM ingredient_merge_deleted_row "
        "WHERE source_table = 'recipe_ingredient' ORDER BY id"
    )):
        _restore_deleted_row(bind, "recipe_ingredient", row_data)

    # 4. Revert every relink strictly by row id -> each reference returns to its origin.
    for source_table, source_row_id, old_ingredient_id in bind.execute(sa.text(
        "SELECT source_table, source_row_id, old_ingredient_id "
        "FROM ingredient_merge_fk_relink ORDER BY id"
    )):
        if source_table not in _RESTORABLE_TABLES:
            raise RuntimeError(f"unexpected relink source_table: {source_table!r}")
        bind.execute(
            sa.text(f"UPDATE {source_table} SET ingredient_id = :old WHERE id = :id"),
            {"old": old_ingredient_id, "id": source_row_id},
        )


def _restore_deleted_row(bind: sa.engine.Connection, table: str, row_data) -> None:
    if table not in _RESTORABLE_TABLES:
        raise RuntimeError(f"unexpected deleted-row source_table: {table!r}")
    bind.execute(
        sa.text(
            f"INSERT INTO {table} SELECT * FROM "
            f"jsonb_populate_record(NULL::{table}, CAST(:data AS jsonb)) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"data": _as_json_text(row_data)},
    )


def _as_json_text(value) -> str:
    """Adapt a JSONB value (a JSON string, or a dict already parsed by the driver) to JSON text."""
    if isinstance(value, str):
        return value
    return json.dumps(value)
