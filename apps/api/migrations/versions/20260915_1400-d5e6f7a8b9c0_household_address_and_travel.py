"""household_address_and_travel: domicilio del hogar + caché de tienda más cercana por cadena

Additive, todo nullable en household (un hogar sin domicilio simplemente no participa del
cálculo de coste de desplazamiento — Fase 2, ver services/plan_comparison.py). Crea también
``household_chain_store``: caché de la tienda más cercana (Overpass/OSM) de cada cadena visible
para el domicilio de un hogar, con ``found=False`` cuando se buscó y no se encontró ninguna
(nunca una distancia inventada). Nada se borra ni se reescribe.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-15 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

_TABLE = "household_chain_store"

_HOUSEHOLD_COLUMNS = (
    "address_text",
    "postal_code",
    "city",
    "latitude",
    "longitude",
    "geocoded_at",
    "geocode_status",
)


def upgrade() -> None:
    op.add_column("household", sa.Column("address_text", sa.Text(), nullable=True))
    op.add_column("household", sa.Column("postal_code", sa.Text(), nullable=True))
    op.add_column("household", sa.Column("city", sa.Text(), nullable=True))
    op.add_column("household", sa.Column("latitude", sa.Numeric(9, 6), nullable=True))
    op.add_column("household", sa.Column("longitude", sa.Numeric(9, 6), nullable=True))
    op.add_column(
        "household", sa.Column("geocoded_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("household", sa.Column("geocode_status", sa.Text(), nullable=True))

    op.create_table(
        _TABLE,
        sa.Column("household_id", sa.BigInteger(), nullable=False),
        sa.Column("retailer_id", sa.BigInteger(), nullable=False),
        sa.Column("store_name", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=True),
        sa.Column("distance_km", sa.Numeric(8, 3), nullable=True),
        sa.Column("found", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["household_id"], ["household.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["retailer_id"], ["retailer.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_household_chain_store_public_id"), _TABLE, ["public_id"], unique=True
    )
    op.create_index(
        "ux_household_chain_store", _TABLE, ["household_id", "retailer_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ux_household_chain_store", table_name=_TABLE)
    op.drop_index(op.f("ix_household_chain_store_public_id"), table_name=_TABLE)
    op.drop_table(_TABLE)

    for column in reversed(_HOUSEHOLD_COLUMNS):
        op.drop_column("household", column)
