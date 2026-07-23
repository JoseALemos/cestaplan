"""Fixture lifecycle (§N) + capture pipeline (§M) — pure logic, no network, no DB.

Covers the versioning gate (rights/review/secrets/PII, synthetic always allowed), synthetic
fixture generation, path safety (raw never into versioned dirs), the 10-record limit and that
capture redacts secrets and imports nothing.
"""

from __future__ import annotations

import pytest

from cestaplan_api.ingestion.providers.fixtures import (
    FixtureManifest,
    build_synthetic_fixture,
    can_version_fixture,
)
from cestaplan_api.ingestion.providers.sample_capture import (
    build_capture_artifacts,
    path_is_safe,
)
from cestaplan_api.ingestion.providers.schema_tools import canonical_structure, structure_report


# --- §N versioning gate ---------------------------------------------------- #
def _manifest(**over) -> FixtureManifest:
    base = {"provider": "parsebot-dia", "fixture_version": "v1", "schema_fingerprint": "abc"}
    base.update(over)
    return FixtureManifest(**base)  # type: ignore[arg-type]


def test_synthetic_fixture_is_always_versionable() -> None:
    m = _manifest(synthetic_structure=True, redistribution_status="synthetic_only")
    assert can_version_fixture(m, has_secrets=False).allowed is True


def test_unknown_rights_blocks_versioning() -> None:
    m = _manifest(redistribution_status="unknown", manually_reviewed=True)
    decision = can_version_fixture(m, has_secrets=False)
    assert decision.allowed is False
    assert any("redistribution_status" in r for r in decision.reasons)


def test_unreviewed_real_fixture_blocked() -> None:
    m = _manifest(
        redistribution_status="approved_for_repository",
        source_rights_status="commercial_use_allowed",
        manually_reviewed=False,
    )
    decision = can_version_fixture(m, has_secrets=False)
    assert decision.allowed is False
    assert "not_manually_reviewed" in decision.reasons


def test_secrets_or_pii_block_versioning() -> None:
    m = _manifest(
        redistribution_status="approved_for_repository",
        source_rights_status="commercial_use_allowed",
        manually_reviewed=True,
    )
    assert can_version_fixture(m, has_secrets=True).allowed is False
    m2 = _manifest(
        redistribution_status="approved_for_repository",
        source_rights_status="commercial_use_allowed",
        manually_reviewed=True,
        contains_personal_data=True,
    )
    assert can_version_fixture(m2, has_secrets=False).allowed is False


def test_fully_cleared_real_fixture_allowed() -> None:
    m = _manifest(
        redistribution_status="approved_for_repository",
        source_rights_status="commercial_use_allowed",
        manually_reviewed=True,
        reviewed_by="ops",
    )
    assert can_version_fixture(m, has_secrets=False).allowed is True


def test_build_synthetic_fixture_preserves_structure() -> None:
    report = structure_report([{"name": "Leche Real", "price": 1.23, "id": 7}])
    records, manifest = build_synthetic_fixture(report, "parsebot-dia")
    assert manifest.synthetic_structure is True
    assert manifest.redistribution_status == "synthetic_only"
    assert records[0]["name"] == "SYNTHETIC" and records[0]["price"] == 0.0
    # structure (fingerprint) preserved
    assert structure_report(records)["schema_fingerprint"] == report["schema_fingerprint"]


# --- §M path safety + capture --------------------------------------------- #
def test_path_safety() -> None:
    assert path_is_safe(".local/provider-samples/dia/raw.json", allow_versioned=False)[0] is True
    assert path_is_safe("/tmp/raw.json", allow_versioned=False)[0] is True
    assert path_is_safe("tests/fixtures/providers/dia/v1.json", allow_versioned=False)[0] is False
    # explicit export flag overrides (only meant for sanitized fixtures)
    assert path_is_safe("tests/fixtures/providers/dia/v1.json", allow_versioned=True)[0] is True


def test_capture_enforces_limit_and_redacts() -> None:
    records = [
        {"sku": "A1", "name": "Leche", "Authorization": "Bearer S", "price": 1.0},
        {"sku": "A2", "name": "Pan", "api_key": "k", "price": 0.9},
    ]
    art = build_capture_artifacts(records, limit=10)
    assert art.record_count == 2
    assert len(art.sha256) == 64 and len(art.schema_fingerprint) == 64
    # secrets redacted in the saved artifacts
    assert art.raw_redacted[0]["Authorization"] == "***REDACTED***"
    assert art.raw_redacted[1]["api_key"] == "***REDACTED***"
    # non-secret data preserved
    assert art.raw_redacted[0]["name"] == "Leche"


def test_capture_refuses_more_than_limit() -> None:
    records = [{"sku": str(i)} for i in range(11)]
    with pytest.raises(ValueError, match="refusing full download"):
        build_capture_artifacts(records, limit=10)


def test_canonical_structure_used_by_capture_is_order_independent() -> None:
    # sanity: the fingerprint the capture reports is structural, not value-based
    a = structure_report([{"a": 1, "b": "x"}])["schema_fingerprint"]
    b = structure_report([{"b": "y", "a": 9}])["schema_fingerprint"]
    assert a == b
    assert canonical_structure({"a": 1}) == canonical_structure({"a": 2})
