"""Schema tools (§L) + redaction (§M) + synthetic generation (§N) — pure logic, no network.

Covers stable canonicalization, reproducible/order-independent fingerprints, type-change and
added/removed detection, drift grading, redaction of keys/tokens/cookies/URL tokens, and
synthetic fixture generation that keeps structure but no real values.
"""

from __future__ import annotations

from cestaplan_api.ingestion.providers.schema_tools import (
    canonical_structure,
    classify_drift,
    diff_structures,
    merge_samples,
    redact,
    schema_fingerprint,
    structure_report,
    synthesize_from_structure,
)


def test_canonicalization_is_stable_and_order_independent() -> None:
    a = {"b": 1, "a": "x", "nested": {"y": True}}
    b = {"a": "x", "nested": {"y": True}, "b": 1}  # different key order
    assert canonical_structure(a) == canonical_structure(b)


def test_fingerprint_reproducible_and_order_independent() -> None:
    a = {"price": 1.0, "name": "Leche", "tags": ["x", "y"]}
    b = {"tags": ["y", "x"], "name": "Otro", "price": 9.9}  # same structure, diff values/order
    assert schema_fingerprint(canonical_structure(a)) == schema_fingerprint(canonical_structure(b))


def test_type_change_changes_fingerprint() -> None:
    a = canonical_structure({"price": 1.0})
    b = canonical_structure({"price": "1.0"})  # float -> str
    assert schema_fingerprint(a) != schema_fingerprint(b)


def test_diff_detects_added_removed_and_type_change() -> None:
    old = canonical_structure({"a": 1, "b": "x"})
    added = canonical_structure({"a": 1, "b": "x", "c": True})
    removed = canonical_structure({"a": 1})
    typed = canonical_structure({"a": "1", "b": "x"})
    assert {c["change"] for c in diff_structures(old, added)} == {"added_field"}
    assert any(c["change"] == "removed_field" for c in diff_structures(old, removed))
    assert any(c["change"].startswith("type_changed") for c in diff_structures(old, typed))


def test_drift_grading() -> None:
    old = canonical_structure({"a": 1, "b": "x"})
    assert classify_drift(diff_structures(old, old)) == "unchanged"
    assert (
        classify_drift(diff_structures(old, canonical_structure({"a": 1, "b": "x", "c": 1})))
        == "additive_compatible"
    )
    assert (
        classify_drift(diff_structures(old, canonical_structure({"a": 1}))) == "breaking"
    )  # removed field
    assert (
        classify_drift(diff_structures(old, canonical_structure({"a": "1", "b": "x"})))
        == "breaking"
    )  # type change


def test_merge_samples_captures_nullability() -> None:
    merged = merge_samples([{"brand": "X"}, {"brand": None}])
    # a field seen as both str and null becomes a "null|str" alternative
    assert "null" in str(merged["brand"])


# --- redaction (§M) -------------------------------------------------------- #
def test_redacts_secret_headers_and_fields() -> None:
    payload = {
        "Authorization": "Bearer SECRET",
        "X-API-Key": "abc123",
        "Cookie": "sid=xyz",
        "Set-Cookie": "sid=xyz",
        "token": "t",
        "password": "p",
        "nested": {"api_key": "k", "safe": "ok"},
    }
    red = redact(payload)
    assert red["Authorization"] == "***REDACTED***"
    assert red["X-API-Key"] == "***REDACTED***"
    assert red["Cookie"] == "***REDACTED***"
    assert red["Set-Cookie"] == "***REDACTED***"
    assert red["token"] == "***REDACTED***"
    assert red["password"] == "***REDACTED***"
    assert red["nested"]["api_key"] == "***REDACTED***"
    assert red["nested"]["safe"] == "ok"


def test_redacts_tokens_in_urls() -> None:
    payload = {"url": "https://api.example.com/x?token=SECRET&page=2"}
    red = redact(payload)
    assert "SECRET" not in red["url"]
    assert "***REDACTED***" in red["url"]
    assert "page=2" in red["url"]  # non-secret params preserved


# --- structure report + synthetic (§M/§N) ---------------------------------- #
def test_structure_report_flags_critical_fields() -> None:
    report = structure_report(
        [{"product_name": "Leche", "price": 1.0, "extra": {"barcode": "84001"}}]
    )
    assert report["record_count"] == 1
    assert len(report["schema_fingerprint"]) == 64
    assert "product_name" in report["critical_fields"]
    assert "price" in report["critical_fields"]
    assert "extra.barcode" in report["critical_fields"]


def test_synthetic_keeps_structure_not_values() -> None:
    real = {"name": "Leche Real", "price": 1.23, "tags": ["a"], "opt": None, "n": 5}
    structure = canonical_structure(real)
    synth = synthesize_from_structure(structure)
    # same fingerprint (structure preserved)...
    assert schema_fingerprint(canonical_structure(synth)) == schema_fingerprint(structure)
    # ...but no real values
    assert synth["name"] == "SYNTHETIC"
    assert synth["price"] == 0.0
    assert synth["opt"] is None
    assert synth["tags"] == ["SYNTHETIC"]
