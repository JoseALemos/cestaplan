"""Safe provider-sample capture pipeline (spec §M).

Turns a small set of raw provider records into safe-to-inspect artifacts, never importing them
and never touching staging/production. The network fetch itself lives in the CLI
(:mod:`cestaplan_api.tools.capture_provider_sample`); this module is the pure, testable core:
limit enforcement, path safety, secret redaction, SHA-256, schema fingerprint, structure
report and a sanitized candidate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cestaplan_api.ingestion.providers.schema_tools import redact, structure_report

# Path fragments that are git-ignored and therefore safe for RAW captures.
_SAFE_FRAGMENTS = (".local/", "provider-samples/", "raw-provider-responses/")
# Versioned areas a raw/sanitized capture must never be written to implicitly.
_VERSIONED_FRAGMENTS = ("tests/", "src/", "data/", "docs/", "apps/", "infra/")


@dataclass(slots=True)
class CaptureArtifacts:
    record_count: int
    sha256: str
    schema_fingerprint: str
    raw_redacted: list[Any]
    sanitized: list[Any]
    report: dict[str, Any]


def path_is_safe(output_path: str, *, allow_versioned: bool) -> tuple[bool, str]:
    """Whether a capture may be written to ``output_path``.

    Raw captures go to git-ignored areas (``.local/`` …). Writing into a versioned area is
    refused unless ``allow_versioned`` (the explicit ``--allow-sanitized-fixture-export`` flag,
    meant only for an already-sanitized fixture).
    """
    p = output_path.replace("\\", "/")
    if allow_versioned:
        return True, "explicitly allowed via --allow-sanitized-fixture-export"
    if p.startswith("/tmp") or any(frag in p for frag in _SAFE_FRAGMENTS):
        return True, "git-ignored capture path"
    if any(frag in p for frag in _VERSIONED_FRAGMENTS):
        return False, "refusing to write a raw capture into a versioned path"
    return True, "path outside known versioned areas"


def build_capture_artifacts(records: list[Any], *, limit: int) -> CaptureArtifacts:
    """Redact + fingerprint + report a small sample. Refuses more than ``limit`` records."""
    if limit <= 0:
        raise ValueError("capture limit must be positive")
    if len(records) > limit:
        raise ValueError(
            f"capture returned {len(records)} records > limit {limit}; refusing full download"
        )
    redacted = [redact(r) for r in records]
    report = structure_report(records)  # structural only; carries no values
    raw_json = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
    sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    return CaptureArtifacts(
        record_count=len(records),
        sha256=sha,
        schema_fingerprint=str(report["schema_fingerprint"]),
        raw_redacted=redacted,
        sanitized=redacted,  # already secret-free — a candidate for manual review (§N)
        report=report,
    )


__all__ = ["CaptureArtifacts", "build_capture_artifacts", "path_is_safe"]
