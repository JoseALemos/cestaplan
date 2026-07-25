"""History-lane temporal invariants (spec §5) + grouping helpers.

A history lane is one interval chain (`valid_from`/`valid_until`) for a variant at a scope/store/
currency/type. This module groups observations into lanes (via the shared lane identity) and reports
temporal-invariant violations. It is READ-ONLY and pure — it never writes — so both the concurrency
tests and the read-only production auditor (`tools/audit_price_history_lanes`) share one definition.

Invariants for the ACTIVE rows of a lane (not rolled back, not disputed):
- at most one OPEN row (`valid_until IS NULL`);
- no overlapping intervals (half-open ``[valid_from, valid_until)``);
- ``valid_from < valid_until`` whenever ``valid_until`` is not null;
- no two rows share the same ``valid_from`` (chronological order is well defined).

DISPUTED rows (same-timestamp conflicts, §7) are excluded from the chain and must carry an empty
``[T, T]`` interval (``valid_from == valid_until``) so they can never be a "current" price.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from typing import Any

from cestaplan_api.models import PriceObservation
from cestaplan_api.services import observation_identity as ident

_DISPUTED = "disputed"


def group_by_lane(rows: list[PriceObservation]) -> dict[str, list[PriceObservation]]:
    lanes: dict[str, list[PriceObservation]] = defaultdict(list)
    for r in rows:
        lanes[ident.price_history_lane_fingerprint(r)].append(r)
    return dict(lanes)


def _is_disputed(r: PriceObservation) -> bool:
    return (r.verification_status or "") == _DISPUTED


def lane_invariant_report(rows: list[PriceObservation]) -> dict[str, Any]:
    """Group ``rows`` into lanes and count temporal-invariant violations. Read-only; counts only.

    The caller chooses the row set (e.g. non-rolled-back rows for a provider). No commercial data is
    returned — only lane counts and violation tallies.
    """
    lanes = group_by_lane(rows)
    report: dict[str, Any] = {
        "lanes": len(lanes),
        "rows": len(rows),
        "lanes_multiple_open": 0,
        "lanes_repeated_timestamp": 0,
        "lanes_overlapping_intervals": 0,
        "rows_non_positive_interval": 0,
        "disputed_rows_non_empty": 0,
    }
    for lane_rows in lanes.values():
        active = [r for r in lane_rows if not _is_disputed(r)]
        disputed = [r for r in lane_rows if _is_disputed(r)]

        if sum(1 for r in active if r.valid_until is None) > 1:
            report["lanes_multiple_open"] += 1

        froms = [r.valid_from for r in active]
        if len(set(froms)) != len(froms):
            report["lanes_repeated_timestamp"] += 1

        ordered = sorted(active, key=lambda r: r.valid_from)
        for a, b in pairwise(ordered):
            if a.valid_until is None or a.valid_until > b.valid_from:
                report["lanes_overlapping_intervals"] += 1
                break

        for r in active:
            if r.valid_until is not None and not (r.valid_from < r.valid_until):
                report["rows_non_positive_interval"] += 1

        for r in disputed:
            if r.valid_until != r.valid_from:
                report["disputed_rows_non_empty"] += 1
    return report


def lane_invariants_hold(rows: list[PriceObservation]) -> bool:
    """True when the row set has zero temporal-invariant violations (test helper, spec §5)."""
    r = lane_invariant_report(rows)
    return (
        r["lanes_multiple_open"] == 0
        and r["lanes_repeated_timestamp"] == 0
        and r["lanes_overlapping_intervals"] == 0
        and r["rows_non_positive_interval"] == 0
        and r["disputed_rows_non_empty"] == 0
    )


__all__ = ["group_by_lane", "lane_invariant_report", "lane_invariants_hold"]
