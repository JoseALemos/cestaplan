"""A production re-crawl of an UNCHANGED price must refresh freshness (revalidate in place).

Regression guard for the freshness bug: a stable price used to age out of the freshness
window between monthly crawls because the production path no-op'd on unchanged prices,
never bumping ``observed_at``. It now revalidates the open row in place (mirroring
``price_history.record_observation``), so every re-crawl keeps the price fresh. A real price
change still appends a new open row and closes the prior interval. Fully synthetic, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.contracts import PriceScope
from cestaplan_api.ingestion.current_price import CurrentPriceService, FreshnessStatus
from cestaplan_api.ingestion.providers.contracts import (
    Availability,
    ExternalCatalogProduct,
    SellUnit,
)
from cestaplan_api.models import CrawlRun, PriceObservation, Retailer
from cestaplan_api.services.observation_persistence import RecordMetrics
from cestaplan_api.services.provider_sync import _append_observation, _upsert_variant

_T1 = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
_T2 = _T1 + timedelta(days=39)  # a monthly-cadence gap, well past the 48h expiry window


def _product(price: str, observed: datetime) -> ExternalCatalogProduct:
    """A national-scope (no postal_code -> store_id None) Mercadona-shaped product."""
    return ExternalCatalogProduct(
        provider="apify-mercadona",
        retailer_slug="mercadona",
        external_product_id="MERC-1",
        product_name="Arroz redondo Hacendado",
        sell_unit=SellUnit.PACKAGE,
        regular_price=Decimal(price),
        currency="EUR",
        price_scope=PriceScope.NATIONAL,
        observed_at=observed,
        availability=Availability.IN_STOCK,
    )


def _setup(db: Session, slug: str):
    retailer = Retailer(slug=slug, name=slug, adapter_key="apify-mercadona")
    db.add(retailer)
    db.flush()
    variant = _upsert_variant(db, retailer.id, _product("1.20", _T1))
    return retailer, variant


def _run(db: Session, retailer_id: int) -> int:
    run = CrawlRun(retailer_id=retailer_id, run_type="prices", status="completed")
    db.add(run)
    db.flush()
    return run.id


def _open_rows(db: Session, variant_id: int) -> list[PriceObservation]:
    return list(
        db.execute(
            select(PriceObservation)
            .where(PriceObservation.product_variant_id == variant_id)
            .order_by(PriceObservation.id)
        ).scalars()
    )


def test_unchanged_price_revalidates_open_row_in_place(db_session: Session) -> None:
    retailer, variant = _setup(db_session, "merc-fresh-1")
    m = RecordMetrics()
    _append_observation(
        db_session, retailer.id, variant, _product("1.20", _T1), _run(db_session, retailer.id),
        False, _T1, m,
    )
    _append_observation(
        db_session, retailer.id, variant, _product("1.20", _T2), _run(db_session, retailer.id),
        False, _T2, m,
    )

    rows = _open_rows(db_session, variant.id)
    # No duplicate history: still a single row, still open, observed_at bumped to the re-crawl.
    assert len(rows) == 1
    assert rows[0].observed_at == _T2
    assert rows[0].valid_from == _T1  # interval START preserved (price began at T1)
    assert rows[0].valid_until is None
    assert m.observations_reused == 1
    assert m.observations_created == 1  # only the first (initial) write


def test_revalidated_price_reads_fresh(db_session: Session) -> None:
    retailer, variant = _setup(db_session, "merc-fresh-2")
    m = RecordMetrics()
    _append_observation(
        db_session, retailer.id, variant, _product("1.20", _T1), _run(db_session, retailer.id),
        False, _T1, m,
    )
    _append_observation(
        db_session, retailer.id, variant, _product("1.20", _T2), _run(db_session, retailer.id),
        False, _T2, m,
    )
    price = CurrentPriceService().current(
        db_session, variant.id, as_of=_T2 + timedelta(hours=1)
    )
    assert price is not None
    # The re-confirmation kept the price fresh; without the fix its age would be ~39 days
    # (EXPIRED).
    assert price.status is FreshnessStatus.FRESH


def test_price_change_still_appends_new_open_row(db_session: Session) -> None:
    retailer, variant = _setup(db_session, "merc-fresh-3")
    m = RecordMetrics()
    _append_observation(
        db_session, retailer.id, variant, _product("1.20", _T1), _run(db_session, retailer.id),
        False, _T1, m,
    )
    _append_observation(
        db_session, retailer.id, variant, _product("1.35", _T2), _run(db_session, retailer.id),
        False, _T2, m,
    )
    rows = _open_rows(db_session, variant.id)
    assert len(rows) == 2  # prior interval closed + new open row
    prior, current = rows
    assert prior.valid_until == _T2 and prior.amount == Decimal("1.20")
    assert current.valid_until is None and current.amount == Decimal("1.35")
    assert m.observations_created == 2 and m.observations_reused == 0
