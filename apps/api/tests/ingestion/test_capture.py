"""RawCaptureRepository tests — live Postgres (rolled back), NO network.

Covers spec §21 raw-capture behaviour: headers are redacted and secrets are never stored,
retention policy is chosen by outcome (error->extended, changed->medium, unchanged->short),
an unchanged response does not re-store a body, the body may be gzip-compressed, the
conditional-GET validators of the prior capture are returned, and expired captures are pruned.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.capture import (
    RETENTION_EXTENDED,
    RETENTION_MEDIUM,
    RETENTION_SHORT,
    RawCaptureRepository,
    retention_policy_for,
)
from cestaplan_api.ingestion.http_fetcher import HttpFetchResult
from cestaplan_api.models import ProductVariant, RawCapture

_SETTINGS = Settings(raw_capture_retention_days=30)


def _repo(session: Session) -> RawCaptureRepository:
    return RawCaptureRepository(session, settings=_SETTINGS)


def _changed_result(body: bytes = b'{"price": 195}') -> HttpFetchResult:
    import hashlib

    return HttpFetchResult(
        url="https://prices.example.com/p/1",
        ok=True,
        status_code=200,
        headers={
            "content-type": "application/json",
            "etag": '"v2"',
            "last-modified": "Wed, 22 Jul 2026 06:00:00 GMT",
            # Secrets that must NEVER reach the DB (already redacted by the fetcher, but the
            # repository re-redacts defensively).
            "set-cookie": "session=leak; Path=/",
            "authorization": "Bearer leak",
        },
        content=body,
        content_type="application/json",
        body_hash=hashlib.sha256(body).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# Retention policy selection
# --------------------------------------------------------------------------- #


def test_retention_policy_by_outcome() -> None:
    changed = _changed_result()
    assert retention_policy_for(changed) == RETENTION_MEDIUM

    unchanged = HttpFetchResult(url="u", status_code=304, not_modified=True, from_cache=True)
    assert retention_policy_for(unchanged) == RETENTION_SHORT

    errored = HttpFetchResult(url="u", status_code=500, error="http_500")
    assert retention_policy_for(errored) == RETENTION_EXTENDED

    blocked = HttpFetchResult(url="u", status_code=403, is_block_page=True, error="block_page")
    assert retention_policy_for(blocked) == RETENTION_EXTENDED


# --------------------------------------------------------------------------- #
# Storing captures
# --------------------------------------------------------------------------- #


def test_changed_capture_stores_compressed_body_and_medium_retention(
    db_session: Session, variant: ProductVariant
) -> None:
    result = _changed_result()
    capture = _repo(db_session).store(
        result, retailer_id=variant.retailer_id, source_url=result.url
    )
    db_session.refresh(capture)

    assert capture.retention_policy == RETENTION_MEDIUM
    assert capture.content_encoding == "gzip"
    assert capture.body_data is not None
    assert gzip.decompress(capture.body_data) == b'{"price": 195}'
    assert capture.body_hash == result.body_hash
    assert capture.is_block_page is False
    # expires_at ~ captured_at + retention_days.
    assert capture.expires_at is not None
    delta = capture.expires_at - capture.captured_at
    assert abs(delta - timedelta(days=30)) < timedelta(seconds=5)


def test_headers_are_redacted_and_secrets_never_stored(
    db_session: Session, variant: ProductVariant
) -> None:
    result = _changed_result()
    capture = _repo(db_session).store(
        result, retailer_id=variant.retailer_id, source_url=result.url
    )
    db_session.refresh(capture)

    headers = capture.response_headers or {}
    assert headers["set-cookie"] == "REDACTED"
    assert headers["authorization"] == "REDACTED"
    assert headers["etag"] == '"v2"'  # non-secret validator preserved

    # No secret value survives anywhere in the stored row.
    serialized = repr(headers) + repr(capture.body_data)
    assert "leak" not in serialized


def test_uncompressed_body_when_disabled(
    db_session: Session, variant: ProductVariant
) -> None:
    result = _changed_result(body=b"raw-bytes")
    capture = _repo(db_session).store(
        result,
        retailer_id=variant.retailer_id,
        source_url=result.url,
        compress=False,
    )
    db_session.refresh(capture)
    assert capture.content_encoding is None
    assert capture.body_data == b"raw-bytes"


def test_unchanged_capture_skips_body_and_short_retention(
    db_session: Session, variant: ProductVariant
) -> None:
    result = HttpFetchResult(
        url="https://prices.example.com/p/1",
        status_code=304,
        not_modified=True,
        from_cache=True,
        body_hash="cachedhash",
        headers={"etag": '"v2"'},
    )
    capture = _repo(db_session).store(
        result, retailer_id=variant.retailer_id, source_url=result.url
    )
    db_session.refresh(capture)
    assert capture.retention_policy == RETENTION_SHORT
    assert capture.body_data is None  # unchanged -> body not re-stored
    assert capture.body_hash == "cachedhash"


def test_error_capture_uses_extended_retention(
    db_session: Session, variant: ProductVariant
) -> None:
    result = HttpFetchResult(
        url="https://prices.example.com/p/1",
        status_code=500,
        error="http_500",
        body_hash="",
    )
    capture = _repo(db_session).store(
        result, retailer_id=variant.retailer_id, source_url=result.url
    )
    db_session.refresh(capture)
    assert capture.retention_policy == RETENTION_EXTENDED


def test_block_page_capture_flagged_and_extended(
    db_session: Session, variant: ProductVariant
) -> None:
    result = HttpFetchResult(
        url="https://prices.example.com/p/1",
        status_code=403,
        is_block_page=True,
        error="block_page",
        content=b"<html>captcha</html>",
        body_hash="h",
    )
    capture = _repo(db_session).store(
        result, retailer_id=variant.retailer_id, source_url=result.url
    )
    db_session.refresh(capture)
    assert capture.is_block_page is True
    assert capture.retention_policy == RETENTION_EXTENDED


# --------------------------------------------------------------------------- #
# Conditional-GET validators
# --------------------------------------------------------------------------- #


def test_prior_conditional_returns_latest_validators(
    db_session: Session, variant: ProductVariant
) -> None:
    repo = _repo(db_session)
    url = "https://prices.example.com/p/1"

    old = _changed_result(body=b"old")
    repo.store(
        old,
        retailer_id=variant.retailer_id,
        source_url=url,
        captured_at=datetime.now(UTC) - timedelta(hours=2),
    )
    new = _changed_result(body=b"new")
    repo.store(
        new,
        retailer_id=variant.retailer_id,
        source_url=url,
        captured_at=datetime.now(UTC),
    )

    prior = repo.prior_conditional(retailer_id=variant.retailer_id, source_url=url)
    assert prior is not None
    assert prior.etag == '"v2"'
    assert prior.last_modified == "Wed, 22 Jul 2026 06:00:00 GMT"
    assert prior.body_hash == new.body_hash  # the most recent capture's hash


def test_prior_conditional_none_when_no_capture(
    db_session: Session, variant: ProductVariant
) -> None:
    prior = _repo(db_session).prior_conditional(
        retailer_id=variant.retailer_id, source_url="https://prices.example.com/never"
    )
    assert prior is None


# --------------------------------------------------------------------------- #
# Retention cleanup
# --------------------------------------------------------------------------- #


def test_cleanup_expired_removes_only_expired(
    db_session: Session, variant: ProductVariant
) -> None:
    repo = _repo(db_session)
    now = datetime.now(UTC)

    expired = _changed_result(body=b"expired")
    cap_expired = repo.store(
        expired,
        retailer_id=variant.retailer_id,
        source_url="https://prices.example.com/old",
    )
    # Force it into the past.
    cap_expired.expires_at = now - timedelta(days=1)
    db_session.flush()

    fresh = _changed_result(body=b"fresh")
    repo.store(
        fresh,
        retailer_id=variant.retailer_id,
        source_url="https://prices.example.com/new",
    )

    removed = repo.cleanup_expired(now=now)
    assert removed == 1
    remaining = db_session.execute(
        select(RawCapture.source_url).where(RawCapture.retailer_id == variant.retailer_id)
    ).scalars().all()
    assert remaining == ["https://prices.example.com/new"]
