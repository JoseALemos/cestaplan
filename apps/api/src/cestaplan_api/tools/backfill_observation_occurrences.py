"""Backfill Layer B: one provenance occurrence per historical PriceObservation (spec §5).

For every existing :class:`PriceObservation` this creates exactly ONE
:class:`PriceObservationOccurrence` built from that observation's OWN metadata (crawl_run_id,
raw_capture_id, source_id/source_url, connector/parser version, confidence, verification status,
imported_at). It is:

- idempotent: an observation that already has the equivalent occurrence is skipped, so re-running
  never duplicates provenance;
- non-destructive: it NEVER deletes or mutates a PriceObservation — provenance is only added;
- honest: it never invents a ``provider_code`` (the historical row does not carry one) and records
  observations whose provenance is insufficient/ambiguous instead of guessing.

``--dry-run`` reports what WOULD happen and writes nothing; ``--apply`` performs the additive
backfill in a single transaction. There is no delete path here by construction.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import (
    PriceObservation,
    PriceObservationOccurrence,
    Retailer,
)

_TOOL_VERSION = "1.0.0"

# The occurrence-identity fields used for the idempotent skip (must match observation_persistence).
_OCC_IDENTITY = (
    "price_observation_id",
    "provider_code",
    "source_id",
    "crawl_run_id",
    "raw_capture_id",
    "connector_version",
    "parser_version",
)


def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def _eq(column, value):
    return column.is_(None) if value is None else column == value


def _occurrence_values(obs: PriceObservation) -> dict[str, Any]:
    """The occurrence derived from an observation's OWN metadata. ``provider_code`` stays None —
    the historical row never carried one and we do not invent it (spec §5)."""
    return {
        "price_observation_id": obs.id,
        "provider_code": None,
        "source_id": obs.source_id,
        "source_url": obs.source_url,
        "crawl_run_id": obs.crawl_run_id,
        "raw_capture_id": obs.raw_capture_id,
        "connector_version": obs.connector_version,
        "parser_version": obs.parser_version,
        "imported_at": obs.imported_at,
        "confidence_score": obs.confidence_score,
        "verification_status": obs.verification_status,
        "evidence_fingerprint": None,
    }


def _has_matching_occurrence(db: Session, values: dict[str, Any]) -> bool:
    conditions = [_eq(getattr(PriceObservationOccurrence, f), values[f]) for f in _OCC_IDENTITY]
    return (
        db.scalar(select(PriceObservationOccurrence.id).where(and_(*conditions)).limit(1))
        is not None
    )


def _is_ambiguous(obs: PriceObservation) -> bool:
    """No crawl run, no raw capture and no source -> provenance is insufficient/ambiguous."""
    return obs.crawl_run_id is None and obs.raw_capture_id is None and obs.source_id is None


def backfill(
    db: Session, provider_code: str | None = None, *, apply: bool = False
) -> dict[str, Any]:
    """Scan observations and (when ``apply``) create the missing provenance occurrences.

    Returns a report with the exact counts required by spec §5. ``deletions`` is always 0.
    """
    result: dict[str, Any] = {
        "tool_version": _TOOL_VERSION,
        "provider_code": provider_code,
        "applied": apply,
        "observations_scanned": 0,
        "occurrences_created": 0,
        "occurrences_already_present": 0,
        "observations_without_provenance": 0,
        "ambiguous_provenance": 0,
        "conflicts": 0,
        "deletions": 0,  # invariant: this tool never deletes a PriceObservation
    }

    stmt = select(PriceObservation)
    if provider_code is not None:
        retailer_id = _retailer_id(db, provider_code)
        result["retailer_id"] = retailer_id
        if retailer_id is None:
            return result
        stmt = stmt.where(PriceObservation.retailer_id == retailer_id)

    for obs in db.execute(stmt).scalars():
        result["observations_scanned"] += 1
        if _is_ambiguous(obs):
            result["observations_without_provenance"] += 1
            result["ambiguous_provenance"] += 1
        values = _occurrence_values(obs)
        if _has_matching_occurrence(db, values):
            result["occurrences_already_present"] += 1
            continue
        result["occurrences_created"] += 1
        if apply:
            db.add(PriceObservationOccurrence(**values))

    if apply:
        db.flush()

    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default=None, help="restrict to one provider/retailer slug")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing (default)")
    p.add_argument("--apply", action="store_true", help="perform the additive backfill")
    args = p.parse_args(argv)

    apply = bool(args.apply) and not args.dry_run
    with SessionLocal() as db:
        report = backfill(db, args.provider, apply=apply)
        if apply:
            db.commit()
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
