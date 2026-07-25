"""Carrefour semantic-compatibility contract (spec §1-§6).

Two-layer model, so an unstable RAW fingerprint never blocks a semantically-unchanged capture:

* **raw_schema_fingerprint** — the exact, STRICT ``schema_tools`` fingerprint over the mapper's
  ``required_core`` (unchanged, kept for audit). It legitimately varies between captures because
  optional fields (ean / promotion / loyalty) are present in some batches and absent/null in others.
  That raw variation is recorded, never used to gate.
* **semantic contract** (this module, Carrefour-ONLY) — decides compatibility from the MEANING of
  the fields, independent of how many products in a batch carry an optional field. ``schema_tools``
  is NOT relaxed globally.

The mapper processes a batch only when the contract is ``compatible`` or
``compatible_with_coverage_loss``; ``review_required`` / ``breaking`` block it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum

CONTRACT_VERSION = "carrefour-semantic-v1"


class SemanticCompatibility(StrEnum):
    COMPATIBLE = "compatible"
    COMPATIBLE_WITH_COVERAGE_LOSS = "compatible_with_coverage_loss"
    REVIEW_REQUIRED = "review_required"
    BREAKING = "breaking"


# Essential fields: a product cannot be mapped without them. product_id must be identifiable, name a
# non-empty string, and at least ONE decodable price (regular_price or promotional_price).
_ESSENTIAL_ID = "product_id"
_ESSENTIAL_NAME = "name"
_PRICE_FIELDS = ("regular_price", "promotional_price")

# Optional fields -> allowed scalar python types when PRESENT (numbers may arrive as int/float/str).
_NUM = (int, float, str)
_STR = (str,)
_OPTIONAL_ALLOWED: dict[str, tuple[type, ...]] = {
    "brand": _STR,
    "promotional_price": _NUM,
    "loyalty_price": _NUM,
    "measure_unit": _STR,
    "package_quantity": _NUM,
    "package_unit": _STR,
    "net_content": _STR,
    "unit_price": _NUM,
    "unit_price_unit": _STR,
    "availability": _STR,
    "ean": (str, int),
    "postal_code": (str, int),
    "sale_point": (str, int),
    "observed_at": _STR,
    "promotion_text": _STR,
    "promotion_start_date": _STR,
    "promotion_end_date": _STR,
    "promotion_conditions": _STR,
    "category": _STR,
    "image_url": _STR,
    "product_url": _STR,
}
_KNOWN_FIELDS = {_ESSENTIAL_ID, _ESSENTIAL_NAME, "regular_price", *_OPTIONAL_ALLOWED}

# Coverage-bearing optional groups. Total absence across the batch is a coverage LOSS (still
# stageable), never a structural break.
_COVERAGE_GROUPS = {
    "price": ("regular_price", "promotional_price"),
    "package": ("net_content", "package_quantity", "package_unit"),
    "barcode": ("ean",),
    "observed_at": ("observed_at",),
    "promotion": ("promotion_text", "promotional_price"),
}


def _is_nested(value: object) -> bool:
    """A dict/list where the contract expects a scalar -> a nesting change."""
    return isinstance(value, (dict, list))


def _decodable_number(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            Decimal(value.replace(",", ".").strip())
        except (InvalidOperation, ValueError):
            return False
        return True
    return False


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _identifiable(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    return str(value).strip() != ""


@dataclass(slots=True)
class ContractResult:
    """Outcome of validating a batch against the Carrefour semantic contract."""

    compatibility: SemanticCompatibility
    reasons: list[str] = field(default_factory=list)
    mappable_records: int = 0
    rejected_records: int = 0
    rejections: list[dict[str, object]] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)  # raw drift, recorded not ignored
    coverage: dict[str, float] = field(default_factory=dict)

    @property
    def processable(self) -> bool:
        return self.compatibility in (
            SemanticCompatibility.COMPATIBLE,
            SemanticCompatibility.COMPATIBLE_WITH_COVERAGE_LOSS,
        )

    @property
    def contract_fingerprint(self) -> str | None:
        """The stable contract identity — emitted ONLY for a compatible capture (same for every
        compatible batch whatever its raw fingerprint); ``None`` for review_required/breaking."""
        return semantic_contract_fingerprint() if self.processable else None

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "compatibility": self.compatibility.value,
            "reasons": self.reasons,
            "mappable_records": self.mappable_records,
            "rejected_records": self.rejected_records,
            "unknown_fields": self.unknown_fields,
            "coverage": self.coverage,
            "semantic_contract_fingerprint": self.contract_fingerprint,
        }


def semantic_contract_fingerprint() -> str:
    """STABLE identity of the contract itself — the SAME for every compatible capture, whatever the
    raw fingerprint. Changes only when the contract definition/version changes."""
    definition = {
        "version": CONTRACT_VERSION,
        "essential": {
            "id": _ESSENTIAL_ID,
            "name": _ESSENTIAL_NAME,
            "price_any_of": list(_PRICE_FIELDS),
        },
        "optional": {
            k: sorted(t.__name__ for t in v) for k, v in sorted(_OPTIONAL_ALLOWED.items())
        },
    }
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def semantic_schema_projection(records: list[dict]) -> dict[str, object]:
    """Project a batch to its semantic essentials: presence/type of essentials and the ALLOWED-ness
    of every present optional field. Deliberately independent of how many records carry an optional
    field, so a promo-heavy and a promo-free batch project the same when both are compatible."""
    optional_types: dict[str, set[str]] = {}
    essential_types: dict[str, set[str]] = {_ESSENTIAL_ID: set(), _ESSENTIAL_NAME: set()}
    for r in records:
        for k in (_ESSENTIAL_ID, _ESSENTIAL_NAME):
            if k in r and r[k] is not None:
                essential_types[k].add(type(r[k]).__name__)
        for k, v in r.items():
            if k in _OPTIONAL_ALLOWED and v is not None:
                optional_types.setdefault(k, set()).add(type(v).__name__)
    return {
        "essential": {k: sorted(v) for k, v in essential_types.items()},
        "optional": {k: sorted(v) for k, v in sorted(optional_types.items())},
    }


def _coverage(records: list[dict]) -> dict[str, float]:
    n = len(records) or 1
    out: dict[str, float] = {}
    for group, fields in _COVERAGE_GROUPS.items():
        covered = sum(
            1 for r in records if any(r.get(f) not in (None, "") for f in fields)
        )
        out[group] = round(covered / n, 4)
    return out


def _record_price_ok(r: dict) -> bool:
    return any(_decodable_number(r.get(f)) for f in _PRICE_FIELDS)


def validate_semantic_contract(records: list[dict]) -> ContractResult:
    """Classify a batch. Structural anomalies on essentials -> breaking; on optionals -> review;
    a per-record missing id/price rejects that record (structure still recognized)."""
    if not records:
        return ContractResult(SemanticCompatibility.COMPATIBLE, ["empty_batch"])

    reasons: list[str] = []
    structural_breaking = False
    review_required = False
    unknown_fields: set[str] = set()
    mappable = 0
    rejections: list[dict[str, object]] = []

    for idx, r in enumerate(records):
        if not isinstance(r, dict):
            structural_breaking = True
            reasons.append(f"record[{idx}] is not an object")
            continue

        # Essential structural checks: a nested id/name/price is a broken structure (breaking).
        for ess in (_ESSENTIAL_ID, _ESSENTIAL_NAME, "regular_price", "promotional_price"):
            if ess in r and _is_nested(r[ess]):
                structural_breaking = True
                reasons.append(f"essential {ess} is nested (object/array) — structure broken")

        # Optional fields: disallowed type OR nesting -> review_required (ambiguous, needs a human).
        for k, v in r.items():
            if k not in _KNOWN_FIELDS:
                unknown_fields.add(k)  # raw drift — recorded, never silently ignored
                continue
            if k not in _OPTIONAL_ALLOWED or v is None:
                continue
            if _is_nested(v) or not isinstance(v, _OPTIONAL_ALLOWED[k]):
                review_required = True
                reasons.append(f"optional {k} present with unexpected type {type(v).__name__}")

        # Per-record mappability (rejection, NOT a contract break): id + name + a decodable price.
        problems = []
        if not _identifiable(r.get(_ESSENTIAL_ID)):
            problems.append("missing_or_unidentifiable_product_id")
        if not _nonempty_str(r.get(_ESSENTIAL_NAME)):
            problems.append("missing_or_empty_name")
        if not _record_price_ok(r):
            problems.append("no_decodable_price")
        if problems:
            rejections.append({"index": idx, "problems": problems})
        else:
            mappable += 1

    coverage = _coverage(records)

    if structural_breaking:
        compat = SemanticCompatibility.BREAKING
    elif review_required:
        compat = SemanticCompatibility.REVIEW_REQUIRED
    elif coverage["price"] > 0 and (coverage["package"] == 0 or coverage["barcode"] == 0):
        compat = SemanticCompatibility.COMPATIBLE_WITH_COVERAGE_LOSS
        reasons.append("optional coverage loss (package/barcode) — stageable, not production-ready")
    else:
        compat = SemanticCompatibility.COMPATIBLE

    return ContractResult(
        compatibility=compat,
        reasons=reasons,
        mappable_records=mappable,
        rejected_records=len(rejections),
        rejections=rejections,
        unknown_fields=sorted(unknown_fields),
        coverage=coverage,
    )


__all__ = [
    "CONTRACT_VERSION",
    "ContractResult",
    "SemanticCompatibility",
    "semantic_contract_fingerprint",
    "semantic_schema_projection",
    "validate_semantic_contract",
]
