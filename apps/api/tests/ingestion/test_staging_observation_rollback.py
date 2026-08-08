"""Logical rollback of orphan STAGING observations (spec §T).

The per-run rollback (``rollback_sync``) can only reach observations tied to a ``crawl_run``.
Discovery creates staging observations with ``crawl_run_id=None`` — this tool reaches exactly
those. It NEVER deletes: matches are marked ``rolled_back_at``/``rolled_back_by`` and closed.
The hard safeguard is ``staging_only is True``: a productive observation is never touched, no
matter the filters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    CrawlRun,
    ExternalProduct,
    PriceObservation,
    Product,
    ProductVariant,
    Retailer,
    Store,
    User,
)
from cestaplan_api.services.price_rollback import rollback_staging_observations

T0 = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)


@pytest.fixture()
def scene(db_session: Session):
    retailer = Retailer(slug="sr-ret", name="SR", adapter_key="test", is_synthetic=True)
    db_session.add(retailer)
    db_session.flush()
    store = Store(retailer_id=retailer.id, name="SR Store", is_synthetic=True)
    product = Product(name="SR Product", is_synthetic=True)
    db_session.add_all([store, product])
    db_session.flush()
    external = ExternalProduct(retailer_id=retailer.id, external_id="SR-1")
    db_session.add(external)
    db_session.flush()
    pv = ProductVariant(
        product_id=product.id,
        retailer_id=retailer.id,
        external_product_id=external.id,
        display_name="SR Variant",
    )
    db_session.add(pv)
    run = CrawlRun(retailer_id=retailer.id, run_type="discovery", status="completed")
    db_session.add(run)
    db_session.flush()
    return retailer, pv, run


def _obs(db, retailer, pv, *, scope, staging=True, crawl_run_id=None, valid_until=None):
    o = PriceObservation(
        retailer_id=retailer.id,
        product_variant_id=pv.id,
        price_scope=scope,
        price_type="regular",
        amount=Decimal("1.00"),
        currency="EUR",
        observed_at=T0,
        imported_at=T0,
        valid_from=T0,
        valid_until=valid_until,
        confidence_score=Decimal("1.0"),
        staging_only=staging,
        crawl_run_id=crawl_run_id,
    )
    db.add(o)
    db.flush()
    return o


def _all_ids(db, retailer):
    return set(
        db.execute(
            select(PriceObservation.id).where(
                PriceObservation.retailer_id == retailer.id
            )
        ).scalars()
    )


def test_dry_run_writes_nothing(scene, db_session: Session) -> None:
    retailer, pv, _run = scene
    _obs(db_session, retailer, pv, scope="unknown")
    _obs(db_session, retailer, pv, scope="national")
    report = rollback_staging_observations(
        db_session, retailer_slug="sr-ret", scope="unknown", only_orphan=True
    )
    assert report.applied is False
    assert report.matched == 1
    assert report.invalidated == 0
    assert report.sample and report.sample[0]["scope"] == "unknown"
    # Nothing was written: no row carries rolled_back_at.
    marked = db_session.scalar(
        select(func.count()).select_from(PriceObservation).where(
            PriceObservation.retailer_id == retailer.id,
            PriceObservation.rolled_back_at.isnot(None),
        )
    )
    assert marked == 0


def test_apply_targets_only_orphan_unknown_and_spares_the_rest(
    scene, db_session: Session
) -> None:
    retailer, pv, run = scene
    actor = User(email="rollback-actor@example.com", password_hash="x")
    db_session.add(actor)
    db_session.flush()
    orphan_unknown = _obs(db_session, retailer, pv, scope="unknown")
    national = _obs(db_session, retailer, pv, scope="national")
    unknown_with_run = _obs(
        db_session, retailer, pv, scope="unknown", crawl_run_id=run.id
    )
    productive = _obs(db_session, retailer, pv, scope="unknown", staging=False)

    before = _all_ids(db_session, retailer)
    report = rollback_staging_observations(
        db_session,
        retailer_slug="sr-ret",
        scope="unknown",
        only_orphan=True,
        apply=True,
        actor_user_id=actor.id,
        now=NOW,
    )
    db_session.flush()

    assert report.applied is True
    assert report.matched == 1
    assert report.invalidated == 1

    # (b) only the staging orphan unknown was rolled back.
    db_session.refresh(orphan_unknown)
    assert orphan_unknown.rolled_back_at == NOW
    assert orphan_unknown.rolled_back_by == actor.id
    assert orphan_unknown.valid_until == orphan_unknown.valid_from  # closed, reversible

    for spared in (national, unknown_with_run):
        db_session.refresh(spared)
        assert spared.rolled_back_at is None

    # (c) the productive observation is NEVER touched.
    db_session.refresh(productive)
    assert productive.rolled_back_at is None
    assert productive.valid_until is None

    # (d) zero DELETE: the set of rows is unchanged.
    assert _all_ids(db_session, retailer) == before


def test_productive_is_untouchable_even_without_filters(
    scene, db_session: Session
) -> None:
    """The staging_only safeguard cannot be bypassed: a productive row is never a match."""
    retailer, pv, _run = scene
    productive = _obs(db_session, retailer, pv, scope="unknown", staging=False)
    report = rollback_staging_observations(
        db_session, retailer_slug="sr-ret", apply=True, now=NOW
    )
    assert report.matched == 0
    db_session.refresh(productive)
    assert productive.rolled_back_at is None


def test_idempotent_second_apply_matches_nothing(scene, db_session: Session) -> None:
    retailer, pv, _run = scene
    _obs(db_session, retailer, pv, scope="unknown")
    first = rollback_staging_observations(
        db_session, retailer_slug="sr-ret", scope="unknown", only_orphan=True,
        apply=True, now=NOW,
    )
    assert first.matched == 1 and first.invalidated == 1
    second = rollback_staging_observations(
        db_session, retailer_slug="sr-ret", scope="unknown", only_orphan=True,
        apply=True, now=NOW,
    )
    assert second.matched == 0 and second.invalidated == 0


def test_unknown_retailer_raises(db_session: Session) -> None:
    with pytest.raises(ValueError, match="unknown retailer"):
        rollback_staging_observations(db_session, retailer_slug="does-not-exist")
