"""History-lane invariant checker + read-only auditor (spec §5/§9). Proves the checker actually
CATCHES violations (not just passes clean data) and that the auditor counts anomalies read-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import PriceObservation
from cestaplan_api.services.price_history_lane import (
    lane_invariant_report,
    lane_invariants_hold,
)
from cestaplan_api.tools import audit_price_history_lanes as auditor
from tests.fixtures.provider_scenarios import seed_test_catalog_product, seed_test_retailer

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


def _row(*, valid_from, valid_until, amount="1.19", status="unverified", variant=1):
    return PriceObservation(
        retailer_id=1, product_variant_id=variant, store_id=None, delivery_zone_id=None,
        price_scope="national", price_type="regular", currency="EUR", staging_only=True,
        amount=Decimal(amount), observed_at=valid_from, valid_from=valid_from,
        valid_until=valid_until, verification_status=status,
    )


def test_clean_chain_holds() -> None:
    rows = [
        _row(valid_from=T0, valid_until=T1, amount="1.19"),
        _row(valid_from=T1, valid_until=T2, amount="1.29"),
        _row(valid_from=T2, valid_until=None, amount="1.39"),  # single open row
    ]
    assert lane_invariants_hold(rows)
    r = lane_invariant_report(rows)
    assert r["lanes"] == 1 and r["lanes_multiple_open"] == 0


def test_two_open_rows_flagged() -> None:
    rows = [
        _row(valid_from=T0, valid_until=None, amount="1.19"),  # open
        _row(valid_from=T1, valid_until=None, amount="1.29"),  # a SECOND open row (the bug)
    ]
    r = lane_invariant_report(rows)
    assert r["lanes_multiple_open"] == 1
    assert not lane_invariants_hold(rows)


def test_overlapping_intervals_flagged() -> None:
    rows = [
        _row(valid_from=T0, valid_until=T2, amount="1.19"),  # [T0, T2)
        _row(valid_from=T1, valid_until=None, amount="1.29"),  # starts inside the previous interval
    ]
    r = lane_invariant_report(rows)
    assert r["lanes_overlapping_intervals"] == 1
    assert not lane_invariants_hold(rows)


def test_non_positive_interval_flagged() -> None:
    rows = [_row(valid_from=T1, valid_until=T0, amount="1.19")]  # valid_until < valid_from
    assert lane_invariant_report(rows)["rows_non_positive_interval"] == 1
    assert not lane_invariants_hold(rows)


def test_disputed_row_must_be_empty() -> None:
    ok = [_row(valid_from=T0, valid_until=T0, amount="1.19", status="disputed")]  # empty [T0,T0]
    assert lane_invariants_hold(ok)
    bad = [_row(valid_from=T0, valid_until=T1, amount="1.19", status="disputed")]  # not empty
    assert lane_invariant_report(bad)["disputed_rows_non_empty"] == 1
    assert not lane_invariants_hold(bad)


def test_distinct_lanes_are_independent() -> None:
    rows = [
        _row(valid_from=T0, valid_until=None, variant=1),  # lane A open
        _row(valid_from=T0, valid_until=None, variant=2),  # lane B open (different variant)
    ]
    assert lane_invariant_report(rows)["lanes"] == 2
    assert lane_invariants_hold(rows)  # one open row EACH, no violation


def test_auditor_is_read_only_and_counts(db_session: Session) -> None:
    retailer = seed_test_retailer(db_session, "carrefour")
    _p, variant = seed_test_catalog_product(db_session, retailer, "AUD-1", name="Aud", price=None)
    # Seed a KNOWN bad lane directly (two open rows) — bypassing record_price_fact on purpose.
    for amount in ("1.19", "1.29"):
        db_session.add(
            PriceObservation(
                retailer_id=retailer.id, product_variant_id=variant.id, price_scope="national",
                price_type="regular", currency="EUR", staging_only=True, amount=Decimal(amount),
                observed_at=T0 + timedelta(hours=int(amount == "1.29")), imported_at=T0,
                valid_from=T0 + timedelta(hours=int(amount == "1.29")),
                confidence_score=Decimal("1.0"),
            )
        )
    db_session.flush()
    report = auditor.audit(db_session, "carrefour", staging_only=True)
    assert report["retailer_id"] == retailer.id
    assert report["lanes_multiple_open"] >= 1  # the two open rows are detected
