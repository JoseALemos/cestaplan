"""Idempotent postal-code -> Store resolution for zonified (delivery-zone) pricing.

Some retailers (Mercadona) publish prices per delivery zone, keyed by postal code. A zone is
modelled as a :class:`Store` row of the chain carrying that ``postal_code``. The write path resolves
``(retailer_id, postal_code)`` to exactly one such Store so every zonified price observation is
stamped with the store that owns it — the value-match that keeps a zone-A price from ever being
served to a zone-B (or no-zone) plan.

Resolution is idempotent: a second call for the same ``(retailer_id, postal_code)`` returns the
same Store, never a duplicate. National providers (no postal code) never call this — their
observations keep ``store_id=None`` (national scope), unchanged.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.models import Store


def resolve_store_for_postal(db: Session, retailer_id: int, postal_code: str) -> Store:
    """Get-or-create the :class:`Store` representing ``retailer``'s delivery zone for a postal code.

    Lookup is by ``(retailer_id, postal_code)`` (the ``ix_store_retailer_postal`` index). The first
    existing zone store is returned; otherwise a new one is created and flushed. Never duplicates a
    zone: repeated resolution within or across syncs returns the same row.
    """
    normalized = postal_code.strip()
    # A zone store is external-code-less (real, imported stores always carry an external_code); the
    # lookup governs exactly the set the ``ux_store_zone_retailer_postal`` unique index enforces, so
    # it never returns — nor collides with — a real store that happens to share the postal code.
    existing = (
        db.execute(
            select(Store)
            .where(
                Store.retailer_id == retailer_id,
                Store.postal_code == normalized,
                Store.external_code.is_(None),
            )
            .order_by(Store.id)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing
    store = Store(
        retailer_id=retailer_id,
        postal_code=normalized,
        name=f"Zona {normalized}",
        is_synthetic=False,
    )
    db.add(store)
    db.flush()
    return store


__all__ = ["resolve_store_for_postal"]
