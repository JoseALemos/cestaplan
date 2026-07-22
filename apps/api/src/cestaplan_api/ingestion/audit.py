"""Source-compliance audit helpers for the ingestion pipeline (FASE A, Task 4).

:class:`SourceAuditService` records and reads the legal/compliance review metadata a
:class:`DataSource` carries — ``legal_status``, ``terms_reviewed_at``, ``robots_reviewed_at``
and free-text ``notes`` — and lists sources with their current legal footing. This is the
paper trail that lets an operator prove each source was reviewed before it was ingested.

Writes flush but never commit; the caller owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.ingestion import LegalStatus
from cestaplan_api.models import DataSource


@dataclass(frozen=True, slots=True)
class SourceReview:
    """The compliance-review metadata of one data source."""

    source_id: int
    slug: str
    name: str
    legal_status: str
    terms_reviewed_at: datetime | None
    robots_reviewed_at: datetime | None
    notes: str | None


class SourceAuditService:
    """Record and read source-compliance review metadata on :class:`DataSource`."""

    def record_review(
        self,
        db: Session,
        source_id: int,
        *,
        legal_status: LegalStatus | None = None,
        terms_reviewed_at: datetime | None = None,
        robots_reviewed_at: datetime | None = None,
        notes: str | None = None,
    ) -> SourceReview:
        """Update the review metadata of a source; only provided fields are changed."""
        source = db.get(DataSource, source_id)
        if source is None:
            raise ValueError(f"DataSource {source_id} not found")
        if legal_status is not None:
            source.legal_status = legal_status.value
        if terms_reviewed_at is not None:
            source.terms_reviewed_at = terms_reviewed_at
        if robots_reviewed_at is not None:
            source.robots_reviewed_at = robots_reviewed_at
        if notes is not None:
            source.notes = notes
        db.flush()
        return self._to_review(source)

    def get_review(self, db: Session, source_id: int) -> SourceReview | None:
        """The review metadata of one source, or ``None`` if it does not exist."""
        source = db.get(DataSource, source_id)
        if source is None:
            return None
        return self._to_review(source)

    def list_sources(self, db: Session) -> list[SourceReview]:
        """Every source with its legal status, ordered by slug."""
        sources = (
            db.execute(select(DataSource).order_by(DataSource.slug)).scalars().all()
        )
        return [self._to_review(source) for source in sources]

    @staticmethod
    def _to_review(source: DataSource) -> SourceReview:
        return SourceReview(
            source_id=source.id,
            slug=source.slug,
            name=source.name,
            legal_status=source.legal_status,
            terms_reviewed_at=source.terms_reviewed_at,
            robots_reviewed_at=source.robots_reviewed_at,
            notes=source.notes,
        )


__all__ = ["SourceAuditService", "SourceReview"]
