"""Versioned schema registry (§L) — filesystem, no network.

Verifies versioning, that an identical schema is not re-stored, that additive vs breaking
changes are graded, that a new (even incompatible) version never overwrites the prior one,
and that drift is 'unavailable' before anything is stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cestaplan_api.ingestion.providers.schema_registry import (
    check_drift,
    latest_meta,
    store_schema,
)

_NOW = datetime(2026, 7, 23, tzinfo=UTC)


def _schema(**fields) -> dict:
    return {"openapi": "3.1.0", "components": {"schemas": {"Price": fields}}}


def test_first_version_and_no_duplicate(tmp_path: Path) -> None:
    s = _schema(price="number", currency="string")
    m1 = store_schema(s, "open-prices", "https://x/openapi.json", base=tmp_path, now=_NOW)
    assert m1.version == 1 and m1.compatibility_status == "unchanged"
    assert m1.provider_schema_version == "v1"
    # storing the identical schema does not create a new version
    m1b = store_schema(s, "open-prices", "https://x/openapi.json", base=tmp_path, now=_NOW)
    assert m1b.version == 1


def test_additive_then_breaking_versions(tmp_path: Path) -> None:
    store_schema(_schema(price="number"), "op", "u", base=tmp_path, now=_NOW)
    m2 = store_schema(_schema(price="number", promo="number"), "op", "u", base=tmp_path, now=_NOW)
    assert m2.version == 2 and m2.compatibility_status == "additive_compatible"
    assert m2.previous_sha256 is not None

    # a type change is breaking, and it is stored as a NEW version (prior kept)
    m3 = store_schema(_schema(price="string", promo="number"), "op", "u", base=tmp_path, now=_NOW)
    assert m3.version == 3 and m3.compatibility_status == "breaking"
    assert (tmp_path / "op" / "v1" / "schema.json").exists()  # prior versions intact
    assert (tmp_path / "op" / "v2" / "schema.json").exists()
    assert latest_meta(tmp_path, "op").version == 3  # type: ignore[union-attr]


def test_drift_unavailable_before_storage(tmp_path: Path) -> None:
    assert (
        check_drift(_schema(price="number"), "new-provider", base=tmp_path)["compatibility_status"]
        == "unavailable"
    )


def test_check_drift_against_stored(tmp_path: Path) -> None:
    store_schema(_schema(price="number"), "op", "u", base=tmp_path, now=_NOW)
    drift = check_drift(_schema(price="string"), "op", base=tmp_path)
    assert drift["compatibility_status"] == "breaking"
    assert drift["changes"]
