"""The single ``RetailerAdapter`` contract (see ``docs/ADAPTER_GUIDE.md``).

An adapter *translates* a concrete source into the canonical model. It never decides
allergy safety, budget or package maths — that is the deterministic engine's job. Read
operations that a source does not support return an explicit "not supported" instead of
fabricating data, and prices are never invented (absence is ``None``, never ``0``).

The value objects here are deliberately plain dataclasses so both the adapters (Task 2)
and the import service (Task 3) can share one normalized shape covering the section-20 CSV
columns → ``Retailer`` / ``Store`` / ``Product`` / ``ProductPrice``.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

# Canonical CSV/JSON column names (docs/DATA_SOURCES.md §6). The importer's default column
# mapping is the identity over this tuple; callers may remap alternative headers onto them.
CANONICAL_COLUMNS: tuple[str, ...] = (
    "retailer_slug",
    "store_external_code",
    "store_province",
    "store_locality",
    "store_postal_code",
    "product_external_id",
    "product_name",
    "brand",
    "category",
    "barcode",
    "package_quantity",
    "package_unit",
    "amount",
    "currency",
    "unit_price",
    "promotion",
    "availability",
    "source_type",
    "source_name",
    "source_url",
    "observed_at",
    "expires_at",
    "confidence_score",
    "verification_status",
)

# A raw parsed row: canonical column name -> raw string value (mapping already applied).
RawRow = dict[str, str]


class NotSupportedError(RuntimeError):
    """Raised when an adapter is asked for an operation it does not implement.

    Distinct from "not found" or "source unavailable": the operation itself is outside the
    adapter's declared :meth:`RetailerAdapter.capabilities`.
    """


class AdapterStatus(enum.StrEnum):
    """Operational status of an adapter (mirrors the reference matrix in the guide)."""

    ACTIVE = "active"
    EXPERIMENTAL = "experimental"
    SKELETON = "skeleton"


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """What an adapter can do — declared, not guessed (guide §2)."""

    supports_search: bool = False
    supports_get_product: bool = False
    supports_get_price: bool = False
    supports_get_availability: bool = False
    supports_store_catalog: bool = False
    requires_network: bool = False
    is_community: bool = False
    default_source_type: str | None = None
    retailers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    """Stable identity of an adapter (guide §2 ``metadata()``)."""

    adapter_key: str
    version: str
    source_type: str | None
    status: AdapterStatus
    enabled: bool
    data_source_slug: str | None = None
    license_code: str | None = None
    attribution_text: str | None = None


@dataclass(frozen=True, slots=True)
class StoreSelector:
    """Multi-level store selection (guide §3), resolved most-specific first."""

    retailer_slug: str
    province: str | None = None
    locality: str | None = None
    postal_code: str | None = None
    store_external_code: str | None = None
    store_public_id: str | None = None
    catalog_date: datetime | None = None
    min_price_coverage: Decimal | None = None


@dataclass(slots=True)
class NormalizedRecord:
    """A source row translated to the canonical model — the unit the importer persists.

    Covers the section-20 CSV columns: identity of retailer/store/product plus one price
    observation. Money and quantities are :class:`~decimal.Decimal`; ``amount`` is the price
    of the *package*, ``unit_price`` (€/kg, €/l) is derived and informative. ``amount`` /
    ``observed_at`` absence is never turned into ``0`` — such a row fails validation upstream.
    """

    retailer_slug: str
    store_external_code: str
    product_external_id: str
    product_name: str
    package_quantity: Decimal
    package_unit: str
    amount: Decimal
    currency: str
    source_type: str
    source_name: str
    observed_at: datetime
    # optional / catalogue enrichment
    store_province: str | None = None
    store_locality: str | None = None
    store_postal_code: str | None = None
    brand: str | None = None
    category: str | None = None
    barcode: str | None = None
    unit_price: Decimal | None = None
    promotion: str | None = None
    availability: str | None = None
    source_url: str | None = None
    expires_at: datetime | None = None
    confidence_score: Decimal | None = None
    verification_status: str = "unverified"

    def to_json(self) -> dict[str, str | None]:
        """Serialise to JSON-safe primitives (Decimals/dates as strings) for storage."""

        def s(value: object) -> str | None:
            return str(value) if value is not None else None

        return {
            "retailer_slug": self.retailer_slug,
            "store_external_code": self.store_external_code,
            "store_province": self.store_province,
            "store_locality": self.store_locality,
            "store_postal_code": self.store_postal_code,
            "product_external_id": self.product_external_id,
            "product_name": self.product_name,
            "brand": self.brand,
            "category": self.category,
            "barcode": self.barcode,
            "package_quantity": s(self.package_quantity),
            "package_unit": self.package_unit,
            "amount": s(self.amount),
            "currency": self.currency,
            "unit_price": s(self.unit_price),
            "promotion": self.promotion,
            "availability": self.availability,
            "source_type": self.source_type,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "confidence_score": s(self.confidence_score),
            "verification_status": self.verification_status,
        }

    @classmethod
    def from_json(cls, data: dict[str, str | None]) -> NormalizedRecord:
        """Rebuild a record previously stored by :meth:`to_json` (for two-phase commit)."""

        def dec(value: str | None) -> Decimal | None:
            return Decimal(value) if value is not None else None

        expires_raw = data.get("expires_at")
        return cls(
            retailer_slug=str(data["retailer_slug"]),
            store_external_code=str(data["store_external_code"]),
            product_external_id=str(data["product_external_id"]),
            product_name=str(data["product_name"]),
            package_quantity=Decimal(str(data["package_quantity"])),
            package_unit=str(data["package_unit"]),
            amount=Decimal(str(data["amount"])),
            currency=str(data["currency"]),
            source_type=str(data["source_type"]),
            source_name=str(data["source_name"]),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
            store_province=data.get("store_province"),
            store_locality=data.get("store_locality"),
            store_postal_code=data.get("store_postal_code"),
            brand=data.get("brand"),
            category=data.get("category"),
            barcode=data.get("barcode"),
            unit_price=dec(data.get("unit_price")),
            promotion=data.get("promotion"),
            availability=data.get("availability"),
            source_url=data.get("source_url"),
            expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
            confidence_score=dec(data.get("confidence_score")),
            verification_status=str(data.get("verification_status") or "unverified"),
        )


class RetailerAdapter(ABC):
    """Common contract every store connector implements.

    Subclasses declare identity via class attributes and support via
    :meth:`capabilities`. Read methods default to raising :class:`NotSupportedError`; a
    subclass overrides only what its source really provides. Batch/file adapters expose
    :meth:`parse` to turn raw content into :class:`RawRow` dicts the importer validates.
    """

    #: Unique, stable key linking ``Retailer.adapter_key``, ``DataSource.adapter_key``
    #: and this registry entry.
    adapter_key: str = ""
    #: Dominant ``source_type`` produced by this adapter (or ``None`` for skeletons).
    source_type: str | None = None
    #: Whether the adapter is active. Community/experimental adapters ship ``False``.
    enabled: bool = False

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Declare the operations, retailers and network needs of this adapter."""

    @abstractmethod
    def metadata(self) -> AdapterMetadata:
        """Return the adapter's stable identity and activation state."""

    # --- read operations (default: explicitly unsupported) ------------------- #
    def search_products(
        self, query: str, selector: StoreSelector, filters: dict | None = None
    ) -> list[NormalizedRecord]:
        raise NotSupportedError(f"{self.adapter_key}: search_products no soportado")

    def get_product(
        self, product_ref: str, selector: StoreSelector
    ) -> NormalizedRecord | None:
        raise NotSupportedError(f"{self.adapter_key}: get_product no soportado")

    def get_price(
        self, product_ref: str, selector: StoreSelector
    ) -> NormalizedRecord | None:
        raise NotSupportedError(f"{self.adapter_key}: get_price no soportado")

    def get_availability(self, product_ref: str, selector: StoreSelector) -> str:
        raise NotSupportedError(f"{self.adapter_key}: get_availability no soportado")

    def get_store_catalog(
        self, selector: StoreSelector, cursor: str | None = None
    ) -> list[NormalizedRecord]:
        raise NotSupportedError(f"{self.adapter_key}: get_store_catalog no soportado")


@dataclass(slots=True)
class ParseError:
    """A structural parse problem (before per-row semantic validation)."""

    row: int
    message: str


@dataclass(slots=True)
class ParseResult:
    """Outcome of parsing file content into canonical raw rows."""

    rows: list[RawRow] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
