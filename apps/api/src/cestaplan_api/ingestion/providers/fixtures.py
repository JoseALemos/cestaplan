"""Provider fixture lifecycle: raw -> sanitized candidate -> golden (spec §N).

Three tiers, enforced by rules — never by convention:

- raw sample: original data, NEVER versioned (git-ignored path).
- sanitized candidate: secrets stripped, may still carry restricted data, needs manual review.
- golden fixture: manually reviewed and rights-cleared, preferably synthetic, versioned in
  ``tests/fixtures/providers/<provider>/`` and independent of the live API.

:func:`can_version_fixture` is the gate: a fixture may only enter the repo when its rights and
review are clear and it holds no secrets/PII (a purely synthetic fixture is always allowed).
:func:`build_synthetic_fixture` produces a structural fixture from a schema report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from cestaplan_api.ingestion.providers.schema_tools import synthesize_from_structure

SANITIZER_VERSION = "1"

REDISTRIBUTION_STATES = (
    "unknown",
    "forbidden",
    "internal_only",
    "synthetic_only",
    "approved_for_repository",
)
# Rights states under which a fixture may be committed to the (MIT) repository.
_VERSIONABLE_RIGHTS = frozenset({"synthetic_only", "approved_for_repository"})


@dataclass(slots=True)
class FixtureManifest:
    provider: str
    fixture_version: str
    schema_fingerprint: str
    captured_at: str | None = None
    sanitized_at: str | None = None
    sanitizer_version: str = SANITIZER_VERSION
    manually_reviewed: bool = False
    reviewed_by: str | None = None
    contains_real_product_names: bool = False
    contains_real_prices: bool = False
    contains_personal_data: bool = False
    redistribution_status: str = "unknown"
    source_rights_status: str = "unknown"
    synthetic_structure: bool = False
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VersionDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


def can_version_fixture(manifest: FixtureManifest, *, has_secrets: bool) -> VersionDecision:
    """Decide whether a fixture may be committed to the repository (spec §N gate)."""
    reasons: list[str] = []
    if has_secrets:
        reasons.append("contains_secrets")
    if manifest.contains_personal_data:
        reasons.append("contains_personal_data")

    if manifest.synthetic_structure:
        # A synthetic fixture holds no real data; it only needs to be secret/PII free.
        return VersionDecision(not reasons, reasons)

    if manifest.redistribution_status not in _VERSIONABLE_RIGHTS:
        reasons.append(f"redistribution_status={manifest.redistribution_status}")
    if manifest.source_rights_status in {"unknown", "forbidden"}:
        reasons.append(f"source_rights_status={manifest.source_rights_status}")
    if not manifest.manually_reviewed:
        reasons.append("not_manually_reviewed")
    return VersionDecision(not reasons, reasons)


def build_synthetic_fixture(
    schema_report: dict[str, Any], provider: str, *, fixture_version: str = "v1"
) -> tuple[list[dict[str, Any]], FixtureManifest]:
    """Build a synthetic fixture (structure preserved, all values fake) from a schema report."""
    structure = schema_report.get("structure")
    if structure is None:
        raise ValueError("schema report has no 'structure'")
    record = synthesize_from_structure(structure)
    records = [record if isinstance(record, dict) else {"value": record}]
    manifest = FixtureManifest(
        provider=provider,
        fixture_version=fixture_version,
        schema_fingerprint=str(schema_report.get("schema_fingerprint", "")),
        synthetic_structure=True,
        manually_reviewed=True,  # synthetic data carries nothing to review
        redistribution_status="synthetic_only",
        source_rights_status="synthetic_only",
        contains_real_product_names=False,
        contains_real_prices=False,
        contains_personal_data=False,
        notes="Generated from a schema report; no real values.",
    )
    return records, manifest


__all__ = [
    "REDISTRIBUTION_STATES",
    "SANITIZER_VERSION",
    "FixtureManifest",
    "VersionDecision",
    "build_synthetic_fixture",
    "can_version_fixture",
]
