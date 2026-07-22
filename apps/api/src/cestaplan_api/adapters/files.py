"""File-based adapters: CSV, JSON and single manual entry.

These do not touch the network. They translate uploaded content into canonical
:class:`RawRow` dicts (CSV/JSON) or a normalized record (manual) that the import service
validates and persists. Column mapping lets a caller feed files whose headers differ from
the canonical section-20 names.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from decimal import Decimal

from cestaplan_api.adapters.base import (
    CANONICAL_COLUMNS,
    AdapterCapabilities,
    AdapterMetadata,
    AdapterStatus,
    NormalizedRecord,
    ParseError,
    ParseResult,
    RawRow,
    RetailerAdapter,
)


def _apply_mapping(
    raw: Mapping[str, object], mapping: Mapping[str, str] | None
) -> RawRow:
    """Project a source row onto canonical column names.

    ``mapping`` is ``{canonical_column: source_header}``; the default is the identity
    (the file already uses canonical headers). Missing/empty cells become ``""``.
    """
    row: RawRow = {}
    for canonical in CANONICAL_COLUMNS:
        header = mapping.get(canonical, canonical) if mapping else canonical
        value = raw.get(header)
        row[canonical] = "" if value is None else str(value).strip()
    return row


class CsvRetailerAdapter(RetailerAdapter):
    """Batch import of catalogue/prices from a UTF-8 CSV (docs/DATA_SOURCES.md §6)."""

    adapter_key = "csv"
    source_type = "admin_import"
    enabled = True

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_store_catalog=True,
            requires_network=False,
            default_source_type="admin_import",
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.ACTIVE,
            enabled=self.enabled,
            data_source_slug="admin-csv",
        )

    def parse(
        self, content: str | bytes, mapping: Mapping[str, str] | None = None
    ) -> ParseResult:
        """Parse CSV text into canonical raw rows (no semantic validation here)."""
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        result = ParseResult()
        try:
            reader = csv.DictReader(io.StringIO(text))
        except csv.Error as exc:  # pragma: no cover - malformed dialect
            result.errors.append(ParseError(row=0, message=f"CSV ilegible: {exc}"))
            return result
        if reader.fieldnames is None:
            result.errors.append(ParseError(row=0, message="CSV sin cabecera"))
            return result
        for raw in reader:
            result.rows.append(_apply_mapping(raw, mapping))
        return result


class JsonRetailerAdapter(RetailerAdapter):
    """Import from JSON with the same field model as the CSV adapter."""

    adapter_key = "json"
    source_type = "admin_import"
    enabled = True

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_store_catalog=True,
            requires_network=False,
            default_source_type="admin_import",
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.ACTIVE,
            enabled=self.enabled,
            data_source_slug="admin-json",
        )

    def parse(
        self, content: str | bytes, mapping: Mapping[str, str] | None = None
    ) -> ParseResult:
        """Parse a JSON array (or ``{"rows": [...]}``) into canonical raw rows."""
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        result = ParseResult()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            result.errors.append(ParseError(row=0, message=f"JSON ilegible: {exc}"))
            return result
        if isinstance(payload, dict) and "rows" in payload:
            payload = payload["rows"]
        if not isinstance(payload, list):
            result.errors.append(
                ParseError(row=0, message="JSON debe ser una lista de filas")
            )
            return result
        for index, raw in enumerate(payload, start=1):
            if not isinstance(raw, dict):
                result.errors.append(
                    ParseError(row=index, message="cada fila debe ser un objeto")
                )
                continue
            result.rows.append(_apply_mapping(raw, mapping))
        return result


class ManualRetailerAdapter(RetailerAdapter):
    """A single price introduced by hand by a user for one product/store."""

    adapter_key = "manual"
    source_type = "manual_entry"
    enabled = True

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_get_price=True,
            requires_network=False,
            default_source_type="manual_entry",
        )

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter_key=self.adapter_key,
            version="1.0",
            source_type=self.source_type,
            status=AdapterStatus.ACTIVE,
            enabled=self.enabled,
            data_source_slug="manual-entry",
        )

    def build_record(self, entry: Mapping[str, object]) -> NormalizedRecord:
        """Build one normalized record from a typed manual entry dict.

        Reuses the canonical row shape so a manual entry flows through the same
        validation as an imported row.
        """
        row = _apply_mapping(entry, None)
        if not row.get("source_type"):
            row["source_type"] = self.source_type or "manual_entry"
        from cestaplan_api.services.importer import build_record  # local: avoid cycle

        record = build_record(row)
        if record.errors:  # pragma: no cover - surfaced to caller
            raise ValueError(
                "; ".join(f"{e.field}: {e.message}" for e in record.errors)
            )
        assert record.record is not None
        return record.record

    def build_record_from_values(
        self,
        *,
        retailer_slug: str,
        store_external_code: str,
        product_external_id: str,
        product_name: str,
        package_quantity: Decimal,
        package_unit: str,
        amount: Decimal,
        currency: str,
        observed_at: object,
        **extra: object,
    ) -> NormalizedRecord:
        """Convenience typed constructor for a manual entry."""
        payload: dict[str, object] = {
            "retailer_slug": retailer_slug,
            "store_external_code": store_external_code,
            "product_external_id": product_external_id,
            "product_name": product_name,
            "package_quantity": package_quantity,
            "package_unit": package_unit,
            "amount": amount,
            "currency": currency,
            "source_type": self.source_type,
            "source_name": str(extra.pop("source_name", "Entrada manual")),
            "observed_at": observed_at,
            **extra,
        }
        return self.build_record(payload)
