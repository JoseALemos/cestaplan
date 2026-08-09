"""zone_store_unique_postal_index: prevent duplicate delivery-zone stores under a race

Zonified pricing (Mercadona) models each delivery zone as a ``store`` row carrying its
``postal_code`` but NO ``external_code`` (real, imported stores always carry an external code).
``resolve_store_for_postal`` get-or-creates that zone store; without a unique constraint two
concurrent syncs could insert duplicate zone stores for the same ``(retailer_id, postal_code)``.

This adds an ADDITIVE partial UNIQUE index over ``(retailer_id, postal_code)`` restricted to
external-code-less rows with a postal code — i.e. exactly the zone stores. Real stores (which have
an ``external_code``) and code-less rows without a postal code are untouched, so multiple real
stores may still share a postal code. No column is added and nothing is deleted.

Revision ID: c4d5e6f7a8b9
Revises: c7f4a1b9e2d3
Create Date: 2026-08-09 12:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "c7f4a1b9e2d3"
branch_labels = None
depends_on = None

_INDEX = "ux_store_zone_retailer_postal"
_TABLE = "store"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        ["retailer_id", "postal_code"],
        unique=True,
        postgresql_where="external_code IS NULL AND postal_code IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
