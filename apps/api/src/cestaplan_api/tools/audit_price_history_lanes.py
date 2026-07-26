"""Strictly READ-ONLY audit of history-lane temporal anomalies (spec §9).

Measures, without modifying anything, how many lanes/rows in the current data violate the temporal
invariants — lanes with more than one open row, repeated timestamps, overlapping intervals, and rows
whose ``valid_until <= valid_from``. Intended to be run against production BEFORE any future change,
so the anomalies are quantified first. It only ever SELECTs; it opens no write transaction.

Output is counts only — no product names, prices, URLs or secrets.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import PriceObservation, Retailer
from cestaplan_api.services.price_history_lane import lane_invariant_report


def _retailer_id(db: Session, provider_code: str) -> int | None:
    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def audit(
    db: Session, provider_code: str | None = None, *, staging_only: bool | None = None
) -> dict[str, Any]:
    """Return lane/anomaly counts for the selected observations. Read-only."""
    stmt = select(PriceObservation).where(PriceObservation.rolled_back_at.is_(None))
    result: dict[str, Any] = {"provider_code": provider_code, "staging_only": staging_only}
    if provider_code is not None:
        rid = _retailer_id(db, provider_code)
        result["retailer_id"] = rid
        if rid is None:
            result.update(lane_invariant_report([]))
            return result
        stmt = stmt.where(PriceObservation.retailer_id == rid)
    if staging_only is not None:
        stmt = stmt.where(PriceObservation.staging_only.is_(staging_only))

    rows = list(db.execute(stmt).scalars())
    result.update(lane_invariant_report(rows))
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default=None, help="restrict to one provider/retailer slug")
    p.add_argument(
        "--staging-only",
        choices=["true", "false"],
        default=None,
        help="restrict to staging (true) or production (false) rows",
    )
    args = p.parse_args(argv)
    staging = None if args.staging_only is None else args.staging_only == "true"
    with SessionLocal() as db:
        report = audit(db, args.provider, staging_only=staging)
        db.rollback()  # belt-and-braces: this tool never writes
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
