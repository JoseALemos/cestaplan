"""The --min-age-days cadence gate: a daily trigger only crawls when a refresh is due."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from cestaplan_api.jobs.sync_price_provider import _last_price_crawl_age_days
from cestaplan_api.models import CrawlRun, Retailer


def test_age_is_none_without_a_completed_crawl(db_session: Session) -> None:
    retailer = Retailer(slug="merc-cad-1", name="merc", adapter_key="apify-mercadona")
    db_session.add(retailer)
    db_session.flush()
    assert _last_price_crawl_age_days(db_session, retailer.id) is None


def test_age_reflects_latest_completed_price_crawl(db_session: Session) -> None:
    retailer = Retailer(slug="merc-cad-2", name="merc", adapter_key="apify-mercadona")
    db_session.add(retailer)
    db_session.flush()
    old = CrawlRun(retailer_id=retailer.id, run_type="prices", status="completed")
    old.created_at = datetime.now(UTC) - timedelta(days=40)
    db_session.add(old)
    db_session.flush()

    age = _last_price_crawl_age_days(db_session, retailer.id)
    assert age is not None
    assert 39.0 < age < 41.0  # ~40 days -> a monthly refresh would be due (min-age-days 28)
