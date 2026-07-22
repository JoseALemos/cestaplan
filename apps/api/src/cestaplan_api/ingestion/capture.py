"""Raw-capture persistence for the price-ingestion subsystem (FASE A, spec §21).

A :class:`~cestaplan_api.models.ingestion.RawCapture` is an immutable snapshot of a source
response, kept for reproducibility and re-parsing. This module turns an
:class:`~cestaplan_api.ingestion.http_fetcher.HttpFetchResult` into a stored capture while
enforcing the subsystem's safety rules:

- **Never persist secrets.** Response headers are redacted (``Authorization`` / ``Cookie`` /
  ``Set-Cookie`` / any token-like header) before storage — the raw values never reach the DB.
- **Retention by outcome** (spec §21): an *error* capture is kept the longest (``extended``)
  for debugging, a *changed* body medium (``medium``), an *unchanged* response the shortest
  (``short``) and its body is not re-stored (the prior identical body is already on file).
- **Optional compression.** A stored body may be gzip-compressed (``content_encoding=gzip``).
- **Conditional-GET support.** :meth:`RawCaptureRepository.prior_conditional` reads the last
  capture's ETag / Last-Modified / body hash so the fetcher can issue a conditional request.
- **Retention cleanup.** :meth:`RawCaptureRepository.cleanup_expired` prunes expired rows.
"""

from __future__ import annotations

import gzip
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.http_fetcher import (
    HttpFetchResult,
    PriorCapture,
    expires_from_now,
    redact_headers,
)
from cestaplan_api.models import RawCapture

# Retention-policy labels stored on RawCapture.retention_policy (spec §21).
RETENTION_EXTENDED = "extended"  # errors / block pages — keep longest for debugging
RETENTION_MEDIUM = "medium"  # a changed body — the useful, re-parseable capture
RETENTION_SHORT = "short"  # unchanged — body not re-stored


def retention_policy_for(result: HttpFetchResult) -> str:
    """Choose a retention policy label from a fetch outcome (spec §21)."""
    if result.error is not None or result.is_block_page:
        return RETENTION_EXTENDED
    if result.not_modified or result.from_cache:
        return RETENTION_SHORT
    return RETENTION_MEDIUM


class RawCaptureRepository:
    """Persists and prunes :class:`RawCapture` rows for a SQLAlchemy session."""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()

    def store(
        self,
        result: HttpFetchResult,
        *,
        retailer_id: int,
        source_url: str,
        crawl_run_id: int | None = None,
        store_id: int | None = None,
        request_method: str = "GET",
        parser_version: str | None = None,
        captured_at: datetime | None = None,
        compress: bool = True,
    ) -> RawCapture:
        """Persist ``result`` as a :class:`RawCapture` (flushed, not committed).

        Headers are redacted, secrets are never stored, retention is chosen by outcome and the
        body of an *unchanged* response is not re-stored. Returns the flushed instance.
        """
        moment = captured_at or self._now()
        policy = retention_policy_for(result)

        body_data: bytes | None = None
        content_encoding: str | None = None
        # Only (re)store a body for a fresh, changed response — never for unchanged/no-change.
        if policy != RETENTION_SHORT and result.content is not None:
            if compress:
                body_data = gzip.compress(result.content)
                content_encoding = "gzip"
            else:
                body_data = result.content

        capture = RawCapture(
            crawl_run_id=crawl_run_id,
            retailer_id=retailer_id,
            store_id=store_id,
            source_url=source_url,
            request_method=request_method,
            response_status=result.status_code,
            content_type=result.content_type,
            content_encoding=content_encoding,
            body_hash=result.body_hash or "",
            response_headers=redact_headers(result.headers),
            body_data=body_data,
            captured_at=moment,
            expires_at=expires_from_now(
                self._settings.raw_capture_retention_days, now=moment
            ),
            is_block_page=result.is_block_page,
            retention_policy=policy,
            parser_version=parser_version,
        )
        self._session.add(capture)
        self._session.flush()
        return capture

    def prior_conditional(
        self, *, retailer_id: int, source_url: str
    ) -> PriorCapture | None:
        """Return conditional-GET validators from the most recent capture of ``source_url``.

        ``None`` when no prior capture exists. The returned :class:`PriorCapture` carries the
        stored ETag / Last-Modified (from the redacted headers) and body hash so the fetcher
        can issue an ``If-None-Match`` / ``If-Modified-Since`` request and detect no-change.
        """
        row = self._session.execute(
            select(RawCapture)
            .where(
                RawCapture.retailer_id == retailer_id,
                RawCapture.source_url == source_url,
            )
            .order_by(RawCapture.captured_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        headers = row.response_headers or {}
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        return PriorCapture(
            etag=lowered.get("etag"),
            last_modified=lowered.get("last-modified"),
            body_hash=row.body_hash or None,
        )

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Delete captures whose ``expires_at`` has passed; return the number removed."""
        moment = now or self._now()
        result = self._session.execute(
            delete(RawCapture).where(
                RawCapture.expires_at.is_not(None),
                RawCapture.expires_at < moment,
            )
        )
        self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    def _now(self) -> datetime:
        from datetime import UTC

        return datetime.now(UTC)
