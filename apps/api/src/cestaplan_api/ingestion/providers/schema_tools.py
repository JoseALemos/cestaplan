"""Schema versioning, drift and fingerprinting (spec §L) + redaction/synthetic helpers (§M/§N).

Pure logic, no network and no DB, so the whole group is testable offline:

- ``canonical_structure`` maps a real payload to a stable structural type tree (key order is
  irrelevant; types / nullability / arrays / nested objects are preserved).
- ``schema_fingerprint`` is the SHA-256 of that structure — reproducible and order-independent.
- ``diff_structures`` / ``classify_drift`` detect added/removed fields, type and nullability
  changes and grade the drift (unchanged / additive_compatible / review_required / breaking).
- ``redact`` strips secrets (Authorization/X-API-Key/Cookie/Set-Cookie, tokens in URLs).
- ``synthesize_from_structure`` builds a synthetic payload that keeps structure and types but
  replaces every real value — for golden fixtures that never depend on the live API.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Header/field names whose values are always removed before anything is written to disk.
_SECRET_KEYS = {
    "authorization",
    "x-api-key",
    "x_api_key",
    "api-key",
    "api_key",
    "apikey",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
}
_REDACTED = "***REDACTED***"
# token-like query params in URLs, e.g. ?token=abc or &api_key=xyz
_URL_TOKEN_RE = re.compile(
    r"([?&](?:token|api_key|apikey|access_token|key|signature|sig)=)[^&#\s]+",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Canonical structure + fingerprint (§L)
# --------------------------------------------------------------------------- #
def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def canonical_structure(value: Any) -> Any:
    """Return the structural type tree of ``value`` (values dropped, key order irrelevant).

    Objects -> ``{key: substructure}`` with keys sorted. Arrays -> ``["array", element]`` where
    the element is the merged structure of the items (so heterogeneous arrays are captured).
    Scalars -> their type name. Nullability shows up as a ``null`` alternative.
    """
    kind = _type_name(value)
    if kind == "object":
        return {k: canonical_structure(value[k]) for k in sorted(value)}
    if kind == "array":
        merged: Any = None
        for item in value:
            merged = _merge(merged, canonical_structure(item))
        return ["array", merged]
    return kind


def _merge(a: Any, b: Any) -> Any:
    """Merge two structures (used across array items / samples), keeping both alternatives."""
    if a is None:
        return b
    if b is None:
        return a
    if a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _merge(a.get(k), b.get(k)) for k in sorted(set(a) | set(b))}
    if isinstance(a, list) and isinstance(b, list) and a and b and a[0] == "array":
        return ["array", _merge(a[1], b[1])]
    # differing scalar types -> a sorted "type|type" alternative (stable representation)
    alts = sorted({x for x in (a, b) if isinstance(x, str)})
    return "|".join(alts) if alts else a


def merge_samples(samples: list[Any]) -> Any:
    """Merge the structures of several real samples into one canonical structure."""
    merged: Any = None
    for sample in samples:
        merged = _merge(merged, canonical_structure(sample))
    return merged


def schema_fingerprint(structure: Any) -> str:
    """Reproducible SHA-256 of a canonical structure (independent of key order)."""
    encoded = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Drift (§L)
# --------------------------------------------------------------------------- #
def diff_structures(old: Any, new: Any, path: str = "") -> list[dict[str, str]]:
    """List structural changes between two canonical structures."""
    changes: list[dict[str, str]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            here = f"{path}.{key}" if path else key
            if key not in new:
                changes.append({"path": here, "change": "removed_field"})
            elif key not in old:
                changes.append({"path": here, "change": "added_field"})
            else:
                changes.extend(diff_structures(old[key], new[key], here))
        return changes
    if isinstance(old, list) and isinstance(new, list) and old and new:
        return diff_structures(old[1], new[1], f"{path}[]")
    if old != new:
        changes.append({"path": path or "<root>", "change": f"type_changed:{old}->{new}"})
    return changes


def diff_json(old: Any, new: Any, path: str = "") -> list[dict[str, str]]:
    """Value-aware diff of two JSON documents (for OpenAPI schemas, where type declarations
    live in the *values*). Reports added/removed fields, scalar changes and list changes."""
    changes: list[dict[str, str]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            here = f"{path}.{key}" if path else key
            if key not in new:
                changes.append({"path": here, "change": "removed_field"})
            elif key not in old:
                changes.append({"path": here, "change": "added_field"})
            else:
                changes.extend(diff_json(old[key], new[key], here))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:  # enum/required/param list changed -> needs review
            changes.append({"path": path or "<root>", "change": "list_changed"})
    elif old != new:
        changes.append({"path": path or "<root>", "change": f"type_changed:{old}->{new}"})
    return changes


def classify_drift(changes: list[dict[str, str]]) -> str:
    """Grade drift: unchanged / additive_compatible / review_required / breaking."""
    if not changes:
        return "unchanged"
    kinds = {c["change"] for c in changes}
    if any(c["change"].startswith("type_changed") for c in changes):
        return "breaking"
    if any(c["change"] == "removed_field" for c in changes):
        return "breaking"
    if kinds == {"added_field"}:
        return "additive_compatible"
    return "review_required"


# --------------------------------------------------------------------------- #
# Redaction (§M)
# --------------------------------------------------------------------------- #
def redact(value: Any) -> Any:
    """Recursively strip secret values (by key) and token-like query params in URL strings."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            if key.lower() in _SECRET_KEYS:
                out[key] = _REDACTED
            else:
                out[key] = redact(val)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _URL_TOKEN_RE.sub(r"\1" + _REDACTED, value)
    return value


# --------------------------------------------------------------------------- #
# Structure report + synthetic generation (§M/§N)
# --------------------------------------------------------------------------- #
# Fields worth flagging for manual review when they appear in a captured sample.
_CRITICAL_HINTS = ("price", "amount", "barcode", "ean", "product", "brand", "name", "url")


def structure_report(samples: list[Any]) -> dict[str, Any]:
    """A non-sensitive structural report: fingerprint, field paths, critical fields, counts."""
    structure = merge_samples(samples)
    paths = _paths(structure)
    critical = sorted({p for p in paths if any(hint in p.lower() for hint in _CRITICAL_HINTS)})
    return {
        "record_count": len(samples),
        "schema_fingerprint": schema_fingerprint(structure),
        "field_paths": paths,
        "critical_fields": critical,
        "review_fields": critical,  # same set needs manual review before promotion
        "structure": structure,
    }


def _paths(structure: Any, prefix: str = "") -> list[str]:
    if isinstance(structure, dict):
        out: list[str] = []
        for key in sorted(structure):
            here = f"{prefix}.{key}" if prefix else key
            out.append(here)
            out.extend(_paths(structure[key], here))
        return out
    if isinstance(structure, list) and len(structure) == 2 and structure[0] == "array":
        return _paths(structure[1], f"{prefix}[]")
    return []


_FAKE = {
    "str": "SYNTHETIC",
    "int": 0,
    "float": 0.0,
    "bool": False,
    "null": None,
}


def synthesize_from_structure(structure: Any) -> Any:
    """Build a fully synthetic payload from a canonical structure (no real values kept)."""
    if isinstance(structure, dict):
        return {k: synthesize_from_structure(v) for k, v in structure.items()}
    if isinstance(structure, list) and len(structure) == 2 and structure[0] == "array":
        return [synthesize_from_structure(structure[1])]
    if not isinstance(structure, str):
        return "SYNTHETIC"
    if "|" in structure:  # type alternative -> pick the first type
        structure = structure.split("|")[0]
    if structure == "null":
        return None
    return _FAKE.get(structure, "SYNTHETIC")


__all__ = [
    "canonical_structure",
    "classify_drift",
    "diff_json",
    "diff_structures",
    "merge_samples",
    "redact",
    "schema_fingerprint",
    "structure_report",
    "synthesize_from_structure",
]
