"""Duplicate-candidate report for provider products (spec §Q).

Products are never merged by name alone and never merged automatically. This produces a
*report* of candidate clusters with a recommendation — ``review`` (a human should confirm) or
``do_not_merge`` (distinct product: different retailer, a distinguishing variant, or a
different size). Identity (same retailer + external id) is not a duplicate.

Identification priority (spec §Q): retailer+external_product_id, exact barcode,
retailer+external_variant_id, normalized brand/name/format, then manual review.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct

# Variant markers that must never be auto-merged even under the same barcode/name.
_VARIANT_MARKERS = (
    "sin lactosa",
    "desnatada",
    "semidesnatada",
    "entera",
    "cocido",
    "crudo",
    "congelado",
    "fresco",
    "integral",
    "multipack",
    "pack",
    "bio",
    "eco",
)


@dataclass(slots=True)
class DuplicateCluster:
    basis: str  # "barcode" | "normalized_name"
    key: str
    members: list[str]  # external_product_ids
    recommendation: str  # "review" | "do_not_merge"
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "key": self.key,
            "members": list(self.members),
            "recommendation": self.recommendation,
            "reason": self.reason,
        }


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _norm_name(name: str) -> str:
    """Lowercase, de-accent, drop sizes/punctuation so plain names group together."""
    text = _strip_accents(name.lower())
    text = re.sub(r"\d+([.,]\d+)?\s*(g|kg|mg|ml|l|cl|ud|uds|unidad|unidades)\b", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(sorted(set(text.split())))


def _markers(name: str) -> frozenset[str]:
    low = _strip_accents(name.lower())
    return frozenset(m for m in _VARIANT_MARKERS if m in low)


def _size(product: ExternalCatalogProduct) -> tuple:
    return (product.net_content_quantity, product.net_content_unit)


def _classify(members: list[ExternalCatalogProduct]) -> tuple[str, str]:
    retailers = {m.retailer_slug for m in members}
    if len(retailers) > 1:
        return "do_not_merge", "different_retailers"
    if len({_markers(m.product_name) for m in members}) > 1:
        return "do_not_merge", "distinguishing_variant"
    if len({_size(m) for m in members}) > 1:
        return "do_not_merge", "different_size"
    return "review", "same_product_candidate"


def find_duplicate_candidates(
    products: list[ExternalCatalogProduct],
) -> list[DuplicateCluster]:
    """Report candidate duplicate clusters (never a destructive merge)."""
    clusters: list[DuplicateCluster] = []
    by_barcode: dict[str, list[ExternalCatalogProduct]] = {}
    for product in products:
        if product.barcode:
            by_barcode.setdefault(product.barcode, []).append(product)

    barcode_ids: set[str] = set()
    for barcode, members in by_barcode.items():
        ids = {m.external_product_id for m in members}
        if len(ids) < 2:  # a single identity, not a duplicate
            continue
        recommendation, reason = _classify(members)
        clusters.append(DuplicateCluster("barcode", barcode, sorted(ids), recommendation, reason))
        barcode_ids |= {m.external_product_id for m in members}

    # Name-based candidates are ALWAYS manual review (never merged on a name alone),
    # excluding products already clustered by a shared barcode.
    by_name: dict[str, list[ExternalCatalogProduct]] = {}
    for product in products:
        if product.external_product_id in barcode_ids:
            continue
        by_name.setdefault(_norm_name(product.product_name), []).append(product)
    for key, members in by_name.items():
        ids = {m.external_product_id for m in members}
        if len(ids) < 2 or not key:
            continue
        recommendation, reason = _classify(members)
        if recommendation == "review":
            reason = "name_match_manual_review"
        clusters.append(
            DuplicateCluster("normalized_name", key, sorted(ids), recommendation, reason)
        )
    return clusters


__all__ = ["DuplicateCluster", "find_duplicate_candidates"]
