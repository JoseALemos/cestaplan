"""Invariant test for the ingredient-consolidation Alembic migration.

Drives ``upgrade -> downgrade -> upgrade`` against a throwaway Postgres database created just
for the test (never the shared dev/prod DB). The seed deliberately exercises the cases that a
naive downgrade gets wrong:

* a **3-member** group (``aceite_oliva`` <- ``aceite de oliva`` <- ``aceite de oliva virgen
  extra``),
* a **survivor that already had its own recipe rows** before the merge (``aceitunas``),
* recipes that cite **both a variant and the survivor**, so the fold produces a duplicate line
  that must be **deduped** (and undone exactly),
* a product-mapping **unique-index collision** (dropped + restored) next to a non-colliding one
  (re-pointed), plus a provider mapping and a pantry item that must round-trip.

Assertions are exact per row id (``ingredient_id``, ``canonical_name``, ``quantity``), not just
counts/sums, so an approximate downgrade fails.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from cestaplan_api.config import Settings, get_settings

_BASE = "35d510ebc887"  # the revision this migration chains onto
_API_ROOT = Path(__file__).resolve().parents[2]

_ING = {
    "aceite_oliva": 1001,                 # survivor (exact _SPECS key)
    "aceite de oliva": 1002,              # variant  -> group of 3
    "aceite de oliva virgen extra": 1003, # variant  -> group of 3
    "aceitunas": 1010,                    # survivor (active mapping) with its OWN recipe rows
    "aceituna": 1011,                     # variant
    "sal": 1020,                          # lone canonical ingredient
}


@pytest.fixture()
def ephemeral_db() -> Iterator[sa.engine.url.URL]:
    base = sa.make_url(Settings().database_url)
    tmp_name = f"cestaplan_migtest_{uuid.uuid4().hex[:12]}"
    admin_engine = sa.create_engine(
        base.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{tmp_name}"'))

    tmp_url = base.set(database=tmp_name)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = tmp_url.render_as_string(hide_password=False)
    get_settings.cache_clear()
    try:
        yield tmp_url
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()
        with admin_engine.connect() as conn:
            conn.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": tmp_name},
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{tmp_name}"'))
        admin_engine.dispose()


def _alembic_config() -> Config:
    cfg = Config(str(_API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_ROOT / "migrations"))
    return cfg


def _seed(engine: sa.engine.Engine) -> None:
    with engine.begin() as conn:
        for name, ingredient_id in _ING.items():
            conn.execute(
                sa.text(
                    "INSERT INTO ingredient (id, canonical_name, display_name, is_synthetic, "
                    "public_id) VALUES (:id, :name, :name, true, :pid)"
                ),
                {"id": ingredient_id, "name": name, "pid": uuid.uuid4()},
            )
        for product_id, pname in (
            (2001, "Aceitunas Verdes 1kg"),
            (2002, "Aceite de Oliva 1L"),
            (2003, "AOVE 500ml"),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO product (id, name, is_synthetic, public_id) "
                    "VALUES (:id, :name, true, :pid)"
                ),
                {"id": product_id, "name": pname, "pid": uuid.uuid4()},
            )
        # Product mappings: 4001 drives the group-2 survivor; 4002 is the survivor's own mapping;
        # 4003 (variant, same product as 4002) COLLIDES on re-point -> dropped+restored; 4004
        # (variant, unique product) is re-pointed.
        for map_id, ing_id, product_id in (
            (4001, _ING["aceitunas"], 2001),
            (4002, _ING["aceite_oliva"], 2002),
            (4003, _ING["aceite de oliva"], 2002),
            (4004, _ING["aceite de oliva virgen extra"], 2003),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO ingredient_product_mapping "
                    "(id, ingredient_id, product_id, is_active, public_id) "
                    "VALUES (:id, :ing, :prod, true, :pid)"
                ),
                {"id": map_id, "ing": ing_id, "prod": product_id, "pid": uuid.uuid4()},
            )
        # A provider mapping on a variant -> re-pointed onto the survivor (round-trips on down).
        conn.execute(
            sa.text(
                "INSERT INTO provider_ingredient_mapping "
                "(id, ingredient_id, canonical_ingredient_key, provider_code, retailer_slug, "
                " external_product_id, mapping_method, confidence_score, public_id) "
                "VALUES (:id, :ing, :key, 'demo', 'demo-retailer', 'EXT-1', 'manual', 0.9, :pid)"
            ),
            {"id": 4101, "ing": _ING["aceituna"], "key": "aceitunas", "pid": uuid.uuid4()},
        )

        for recipe_id in (3001, 3002, 3003):
            conn.execute(
                sa.text(
                    "INSERT INTO recipe (id, origin, title, servings, is_public, is_synthetic, "
                    "public_id) VALUES (:id, 'seed', :title, 2, false, true, :pid)"
                ),
                {"id": recipe_id, "title": f"Recipe {recipe_id}", "pid": uuid.uuid4()},
            )
        recipe_ings = [
            # Recipe 3001: one variant + the lone ingredient.
            (5001, 3001, _ING["aceite de oliva"], "aceite de oliva", "10.0000", "ml"),
            (5002, 3001, _ING["sal"], "sal", "5.0000", "g"),
            # Recipe 3002: cites a variant AND the survivor -> dedup after the fold.
            (5003, 3002, _ING["aceite de oliva virgen extra"], "aceite de oliva virgen extra",
             "8.0000", "ml"),
            (5004, 3002, _ING["aceite_oliva"], "aceite_oliva", "2.0000", "ml"),
            # Recipe 3003: survivor's OWN row + a variant -> dedup after the fold.
            (5005, 3003, _ING["aceitunas"], "aceitunas", "20.0000", "g"),
            (5006, 3003, _ING["aceituna"], "aceituna", "15.0000", "g"),
        ]
        for row_id, recipe_id, ingredient_id, name, qty, unit in recipe_ings:
            conn.execute(
                sa.text(
                    "INSERT INTO recipe_ingredient (id, recipe_id, ingredient_id, "
                    "canonical_name, quantity, unit, optional, public_id) "
                    "VALUES (:id, :rid, :ing, :name, :qty, :unit, false, :pid)"
                ),
                {"id": row_id, "rid": recipe_id, "ing": ingredient_id, "name": name,
                 "qty": qty, "unit": unit, "pid": uuid.uuid4()},
            )

        # A household + pantry item on a variant, to exercise the pantry_item relink round-trip.
        conn.execute(
            sa.text(
                "INSERT INTO \"user\" (id, email, password_hash, public_id) "
                "VALUES (:id, :email, 'x', :pid)"
            ),
            {"id": 7001, "email": "migtest@example.com", "pid": uuid.uuid4()},
        )
        conn.execute(
            sa.text(
                "INSERT INTO household (id, name, owner_user_id, public_id) "
                "VALUES (:id, 'H', :owner, :pid)"
            ),
            {"id": 7101, "owner": 7001, "pid": uuid.uuid4()},
        )
        conn.execute(
            sa.text(
                "INSERT INTO pantry_item (id, household_id, ingredient_id, quantity, unit, "
                "public_id) VALUES (:id, :hh, :ing, 3, 'ml', :pid)"
            ),
            {"id": 7201, "hh": 7101, "ing": _ING["aceite de oliva"], "pid": uuid.uuid4()},
        )


# --------------------------------------------------------------------------- #
# Snapshot helpers
# --------------------------------------------------------------------------- #
def _recipe_ingredients(engine: sa.engine.Engine) -> dict[int, tuple[int, str, Decimal]]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT id, ingredient_id, canonical_name, quantity FROM recipe_ingredient"
        )).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _mapping_targets(engine: sa.engine.Engine, table: str) -> dict[int, int]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(f"SELECT id, ingredient_id FROM {table}")).all()
    return {r[0]: r[1] for r in rows}


def _pantry_targets(engine: sa.engine.Engine) -> dict[int, int]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, ingredient_id FROM pantry_item")).all()
    return {r[0]: r[1] for r in rows}


def _quantity_sum(engine: sa.engine.Engine) -> Decimal:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT COALESCE(SUM(quantity), 0) FROM recipe_ingredient")
        ).scalar_one()


def _orphan_fk_count(engine: sa.engine.Engine) -> int:
    total = 0
    with engine.connect() as conn:
        for table in ("recipe_ingredient", "pantry_item", "ingredient_product_mapping",
                      "provider_ingredient_mapping"):
            total += conn.execute(sa.text(
                f"SELECT COUNT(*) FROM {table} t "
                "LEFT JOIN ingredient i ON t.ingredient_id = i.id "
                "WHERE t.ingredient_id IS NOT NULL AND i.id IS NULL"
            )).scalar_one()
    return total


def _table_exists(engine: sa.engine.Engine, table: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
        ).scalar_one() is not None


def _assert_consolidated(engine: sa.engine.Engine) -> None:
    with engine.connect() as conn:
        present = {r[0] for r in conn.execute(sa.text("SELECT id FROM ingredient"))}
        # All three variants are folded away; the survivors + the lone ingredient remain.
        assert _ING["aceite de oliva"] not in present
        assert _ING["aceite de oliva virgen extra"] not in present
        assert _ING["aceituna"] not in present
        assert {_ING["aceite_oliva"], _ING["aceitunas"], _ING["sal"]} <= present

        # Dedup: no recipe cites the same ingredient twice.
        dup = conn.execute(sa.text(
            "SELECT COUNT(*) FROM (SELECT recipe_id, ingredient_id FROM recipe_ingredient "
            "GROUP BY recipe_id, ingredient_id HAVING COUNT(*) > 1) q"
        )).scalar_one()
        assert dup == 0

        # Recipe 3002: the two olive-oil lines collapsed into one carrying the summed quantity.
        b_recipe = conn.execute(sa.text(
            "SELECT ingredient_id, quantity, canonical_name "
            "FROM recipe_ingredient WHERE recipe_id = 3002"
        )).all()
        assert len(b_recipe) == 1
        assert b_recipe[0][0] == _ING["aceite_oliva"]
        assert b_recipe[0][1] == Decimal("10.0000")  # 8 + 2
        assert b_recipe[0][2] == "aceite_oliva"  # normalized to the survivor slug

        # Recipe 3003: survivor's own line absorbed the variant line.
        c_recipe = conn.execute(sa.text(
            "SELECT quantity, canonical_name FROM recipe_ingredient WHERE recipe_id = 3003"
        )).all()
        assert len(c_recipe) == 1
        assert c_recipe[0][0] == Decimal("35.0000")  # 20 + 15
        assert c_recipe[0][1] == "aceitunas"

        # The name-based costing gate invariant is restored: EVERY line that resolves to a
        # fold-target survivor carries that survivor's canonical slug, not the human name.
        name_mismatch = conn.execute(sa.text(
            "SELECT COUNT(*) FROM recipe_ingredient ri JOIN ingredient i "
            "ON ri.ingredient_id = i.id "
            "WHERE ri.ingredient_id IN (:a, :b) AND ri.canonical_name <> i.canonical_name"
        ), {"a": _ING["aceite_oliva"], "b": _ING["aceitunas"]}).scalar_one()
        assert name_mismatch == 0
        # Line 5001 (recipe 3001) was the human-named variant; it now reads as the slug.
        assert conn.execute(sa.text(
            "SELECT canonical_name FROM recipe_ingredient WHERE id = 5001"
        )).scalar_one() == "aceite_oliva"

        # Product-mapping collision: the variant duplicate on product 2002 was dropped.
        assert _mapping_targets(engine, "ingredient_product_mapping") == {
            4001: _ING["aceitunas"],
            4002: _ING["aceite_oliva"],
            4004: _ING["aceite_oliva"],  # re-pointed off the variant
        }
        # Provider mapping re-pointed onto the survivor.
        assert _mapping_targets(engine, "provider_ingredient_mapping") == {
            4101: _ING["aceitunas"]
        }
        # Pantry re-pointed onto the survivor.
        assert _pantry_targets(engine) == {7201: _ING["aceite_oliva"]}

        # Aliases + audits recorded for every variant.
        aliases = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT alias_text, ingredient_id FROM ingredient_alias"
            ))
        }
        assert aliases == {
            "aceite de oliva": _ING["aceite_oliva"],
            "aceite de oliva virgen extra": _ING["aceite_oliva"],
            "aceituna": _ING["aceitunas"],
        }
        assert conn.execute(
            sa.text("SELECT COUNT(*) FROM ingredient_merge_audit")
        ).scalar_one() == 3

        # Every recipe keeps a mandatory ingredient with a resolvable FK.
        for recipe_id in (3001, 3002, 3003):
            resolvable = conn.execute(sa.text(
                "SELECT COUNT(*) FROM recipe_ingredient ri "
                "JOIN ingredient i ON ri.ingredient_id = i.id "
                "WHERE ri.recipe_id = :r AND ri.optional = false"
            ), {"r": recipe_id}).scalar_one()
            assert resolvable >= 1

    assert _orphan_fk_count(engine) == 0


def test_migration_up_down_up_preserves_invariants(
    ephemeral_db: sa.engine.url.URL,
) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, _BASE)

    engine = sa.create_engine(ephemeral_db)
    try:
        _seed(engine)
        before_recipe = _recipe_ingredients(engine)
        before_products = _mapping_targets(engine, "ingredient_product_mapping")
        before_providers = _mapping_targets(engine, "provider_ingredient_mapping")
        before_pantry = _pantry_targets(engine)
        before_sum = _quantity_sum(engine)
        assert len(before_recipe) == 6
        assert _orphan_fk_count(engine) == 0

        # --- upgrade: fractures consolidated, duplicates deduped ---
        command.upgrade(cfg, "head")
        _assert_consolidated(engine)
        assert _quantity_sum(engine) == before_sum  # dedup preserves total quantity

        # --- downgrade: EXACT restoration, per row id (ingredient_id, canonical_name, quantity) ---
        command.downgrade(cfg, _BASE)
        assert _recipe_ingredients(engine) == before_recipe
        # The overwritten canonical_name is restored to the original human name.
        with engine.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT canonical_name FROM recipe_ingredient WHERE id = 5001"
            )).scalar_one() == "aceite de oliva"
        assert _mapping_targets(engine, "ingredient_product_mapping") == before_products
        assert _mapping_targets(engine, "provider_ingredient_mapping") == before_providers
        assert _pantry_targets(engine) == before_pantry
        assert _quantity_sum(engine) == before_sum
        assert _orphan_fk_count(engine) == 0
        assert not _table_exists(engine, "ingredient_alias")
        assert not _table_exists(engine, "ingredient_merge_audit")
        assert not _table_exists(engine, "ingredient_merge_fk_relink")
        assert not _table_exists(engine, "ingredient_merge_deleted_row")

        # --- upgrade again: idempotent, reproduces the consolidated state ---
        command.upgrade(cfg, "head")
        _assert_consolidated(engine)
        assert _quantity_sum(engine) == before_sum
    finally:
        engine.dispose()
