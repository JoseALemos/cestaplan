"""Idempotent bootstrap of canonical ``Retailer`` rows for authorized real chains.

``onboarding.upsert_activation`` creates a ``ProviderActivation`` but NEVER a ``Retailer``, so
production starts with ``RETAILERS=[]`` and no provider sync can target a chain (the sync CLI
resolves the retailer by slug and exits if it is missing). This tool fills exactly that gap and
nothing more:

* get-or-creates the ``Retailer`` (``is_synthetic=False``) for AUTHORIZED chains only, taking the
  slug/provider from the onboarding matrix;
* creates **no** stores, **no** products, **no** prices, and **no** production activation.

It is idempotent (a chain already present is skipped) and asserts that ``Product``/``ProductPrice``
counts are unchanged. Dry-run rolls back; ``--apply`` commits.

    python -m cestaplan_api.tools.bootstrap_retailers --only alcampo --dry-run
    python -m cestaplan_api.tools.bootstrap_retailers --only alcampo --apply
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import get_entry
from cestaplan_api.models import Product, ProductPrice, Retailer

# Real chains an operator has authorized to exist as canonical retailers. Kept explicit so a typo
# can never onboard an unintended chain. (slug -> display name.)
AUTHORIZED_CHAINS: dict[str, str] = {
    "alcampo": "Alcampo",
}


def _counts(db: Session) -> dict[str, int]:
    return {
        "retailers": int(db.scalar(select(func.count()).select_from(Retailer)) or 0),
        "products": int(db.scalar(select(func.count()).select_from(Product)) or 0),
        "product_prices": int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0),
    }


def bootstrap(db: Session, slugs: list[str]) -> list[str]:
    """Get-or-create a canonical Retailer per slug. Returns the slugs actually created."""
    created: list[str] = []
    for slug in slugs:
        if slug not in AUTHORIZED_CHAINS:
            raise ValueError(f"chain {slug!r} is not authorized: {sorted(AUTHORIZED_CHAINS)}")
        existing = db.execute(
            select(Retailer).where(Retailer.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        entry = get_entry(f"parsebot-{slug}")
        adapter_key = entry.provider_code if entry is not None else f"parsebot-{slug}"
        db.add(
            Retailer(
                slug=slug,
                name=AUTHORIZED_CHAINS[slug],
                adapter_key=adapter_key,
                country="ES",
                is_synthetic=False,
            )
        )
        created.append(slug)
    db.flush()
    return created


def run(*, slugs: list[str], apply: bool) -> dict[str, object]:
    with SessionLocal() as db:
        before = _counts(db)
        created = bootstrap(db, slugs)
        after = _counts(db)
        # Hard guard: this tool must never touch products or prices.
        if after["products"] != before["products"] or after["product_prices"] != before[
            "product_prices"
        ]:
            db.rollback()
            raise RuntimeError("bootstrap unexpectedly changed product/price rows; rolled back")
        if apply:
            db.commit()
        else:
            db.rollback()
    return {
        "mode": "apply" if apply else "dry-run",
        "created": created,
        "before": before,
        "after": after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="chain slug to bootstrap (repeatable); defaults to all authorized chains",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    slugs = args.only if args.only else sorted(AUTHORIZED_CHAINS)
    print(json.dumps(run(slugs=slugs, apply=bool(args.apply)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
