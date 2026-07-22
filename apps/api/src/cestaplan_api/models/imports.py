"""Data-import and barcode models: DataImport, ProductBarcode.

``DataImport`` is the batch that :attr:`ProductPrice.import_id` points at. Every price
observation written by an admin CSV/JSON import is tagged with the id of its ``DataImport``
so a whole batch can be logically rolled back (its price rows removed) without touching the
products it created. ``ProductBarcode`` records EAN/UPC codes for a product (populated now so
the Open Food Facts enrichment that lands next has a table to write to).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cestaplan_api.models.base import BaseModel, enum_col
from cestaplan_api.models.catalog import SOURCE_TYPE

# Lifecycle of an import batch (canonical §fuentes e importación).
DATA_IMPORT_STATUS = (
    "pending",  # validated, ready to commit (dry_run flag was false)
    "validating",  # transient while parsing/validating
    "dry_run",  # validated as a dry run; writes nothing
    "committed",  # prices written and tagged with this import's id
    "rolled_back",  # committed prices later removed by a logical rollback
    "failed",  # validation or commit failed fatally
)
DATA_IMPORT_FORMAT = ("csv", "json")


class DataImport(BaseModel):
    """A single admin catalogue/price import batch (CSV or JSON).

    Traceability + reversibility anchor: the ``summary`` JSON holds the per-row errors,
    the aggregate stats and the validated records; the counters mirror those stats. Price
    rows created on commit carry ``import_id = DataImport.id``.
    """

    __tablename__ = "data_import"
    __table_args__ = (
        Index("ix_data_import_status", "status"),
        Index("ix_data_import_created_by", "created_by_user_id"),
    )

    retailer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("retailer.id"))
    data_source_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_source.id")
    )
    source_type: Mapped[str | None] = mapped_column(
        enum_col(*SOURCE_TYPE, name="data_import_source_type")
    )
    status: Mapped[str] = mapped_column(
        enum_col(*DATA_IMPORT_STATUS, name="data_import_status"),
        nullable=False,
        server_default="pending",
    )
    filename: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(
        enum_col(*DATA_IMPORT_FORMAT, name="data_import_format"), nullable=False
    )
    checksum: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ok_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updated_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    summary: Mapped[dict | None] = mapped_column(JSONB)
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id")
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductBarcode(BaseModel):
    """EAN/UPC barcode of a catalogue product.

    A product may carry several barcodes (multipacks / relabels); a barcode is recorded
    once per product (``ux_barcode_product``). Global uniqueness is deliberately NOT
    enforced: the same physical EAN can appear under different retailers' ``Product`` rows,
    each of which is a distinct catalogue article. ``barcode`` is indexed for enrichment
    lookups (e.g. Open Food Facts matching by code).
    """

    __tablename__ = "product_barcode"
    __table_args__ = (
        Index("ux_barcode_product", "product_id", "barcode", unique=True),
        Index("ix_barcode_code", "barcode"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.id", ondelete="CASCADE"), nullable=False
    )
    barcode: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
