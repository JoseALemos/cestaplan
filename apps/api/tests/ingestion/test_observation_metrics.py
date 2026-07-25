"""A.3 two-layer metrics (spec §9): the panel distinguishes unique price facts from provenance
occurrences and never reports repeated confirmations of one fact as different prices."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from cestaplan_api.models import PriceObservation, PriceObservationOccurrence
from cestaplan_api.services.observation_metrics import observation_metrics
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_retailer,
)

PROVIDER = "test_metrics_provider"
T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)


def _obs(db, rid, vid, *, amount):
    o = PriceObservation(
        retailer_id=rid, product_variant_id=vid, price_scope="national", price_type="regular",
        amount=Decimal(amount), currency="EUR", observed_at=T0, imported_at=T0, valid_from=T0,
        confidence_score=Decimal("1.0"), staging_only=True,
    )
    db.add(o)
    db.flush()
    return o


def _occ(db, obs_id, *, crawl=None):
    db.add(
        PriceObservationOccurrence(
            price_observation_id=obs_id, provider_code=PROVIDER, crawl_run_id=crawl, imported_at=T0,
        )
    )
    db.flush()


def test_one_fact_many_occurrences_is_not_many_prices(db_session: Session) -> None:
    retailer = seed_test_retailer(db_session, PROVIDER)
    _p, variant = seed_test_catalog_product(db_session, retailer, "MT-1", name="Metric", price=None)
    fact = _obs(db_session, retailer.id, variant.id, amount="1.19")
    # One economic fact, confirmed by three occurrences (ambiguous: no crawl/capture/source).
    _occ(db_session, fact.id)
    _occ(db_session, fact.id)
    _occ(db_session, fact.id)

    m = observation_metrics(db_session, PROVIDER)
    assert m["unique_price_facts"] == 1
    assert m["staging_observations"] == 1
    assert m["provenance_occurrences"] == 3
    assert m["occurrences_ambiguous_provenance"] == 3
    # Three confirmations of ONE fact are not three prices.
    assert m["duplicate_price_observations_by_fact_identity"] == 0
    assert "confirmed repeatedly" in m["note"]


def test_duplicate_facts_are_reported_as_duplicates(db_session: Session) -> None:
    retailer = seed_test_retailer(db_session, PROVIDER)
    _p, variant = seed_test_catalog_product(db_session, retailer, "MT-2", name="M2", price=None)
    # Two IDENTICAL facts (exact duplicates) -> one is a removable duplicate.
    _obs(db_session, retailer.id, variant.id, amount="1.19")
    _obs(db_session, retailer.id, variant.id, amount="1.19")

    m = observation_metrics(db_session, PROVIDER)
    assert m["staging_observations"] == 2
    assert m["unique_price_facts"] == 1
    assert m["duplicate_price_observations_by_fact_identity"] == 1
    assert m["duplicate_fact_groups"] == 1
    assert m["quality_gate"]["status"] in ("warning", "critical")
    assert "duplicate_price_observations_present" in m["quality_gate"]["reasons"]


def test_clean_provider_gate_is_ok(db_session: Session) -> None:
    retailer = seed_test_retailer(db_session, PROVIDER)
    _p, variant = seed_test_catalog_product(db_session, retailer, "MT-3", name="M3", price=None)
    fact = _obs(db_session, retailer.id, variant.id, amount="1.19")
    _occ(db_session, fact.id, crawl=None)  # single occurrence

    m = observation_metrics(db_session, PROVIDER)
    assert m["unique_price_facts"] == 1
    assert m["duplicate_price_observations_by_fact_identity"] == 0
    # Only ambiguous provenance keeps it at 'warning'; with a verified occurrence it would be ok.
    assert m["quality_gate"]["status"] in ("ok", "warning")
