"""Idempotent bootstrap of canonical ``Retailer`` rows for the authorized real chains.

``onboarding.upsert_activation`` creates a ``ProviderActivation`` but NEVER a ``Retailer``, and no
other production code path creates real (non-synthetic) retailers, so production starts with
``RETAILERS=[]`` and a provider sync has no chain to target. This tool fills exactly that gap and
nothing more:

* get-or-creates the ``Retailer`` (``is_synthetic=False``) for AUTHORIZED chains only, taking the
  slug from :data:`AUTHORIZED_CHAINS` and the ``adapter_key``/provider code from the onboarding
  ``RETAILER_MATRIX`` (so ``mercadona`` correctly resolves to ``apify-mercadona``, not
  ``parsebot-mercadona``);
* creates **no** stores, products, prices, mappings, or production activation;
* is fill-only + idempotent (a chain already present is skipped, admin edits are never overwritten)
  and asserts that ``Product``/``ProductPrice`` counts never change.

``open-prices`` is intentionally NOT a retailer: it is a cross-cutting observation source.

    python -m cestaplan_api.tools.bootstrap_retailers --dry-run --all
    python -m cestaplan_api.tools.bootstrap_retailers --apply --all
    python -m cestaplan_api.tools.bootstrap_retailers --dry-run --provider parsebot-dia
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.onboarding import RETAILER_MATRIX
from cestaplan_api.models import Product, ProductPrice, Retailer

# The real chains an operator has authorized as canonical retailers (slug -> commercial name). Kept
# explicit so a typo can never onboard an unintended chain. open_prices/demo are left out.
AUTHORIZED_CHAINS: dict[str, str] = {
    "alcampo": "Alcampo",
    "dia": "DIA",
    "carrefour": "Carrefour",
    "lidl": "Lidl",
    "aldi": "Aldi",
    "deza": "Deza",
    "mercadona": "Mercadona",
}

# slug -> provider_code, from the single source of truth (the onboarding matrix).
_SLUG_TO_PROVIDER: dict[str, str] = {e.retailer_slug: e.provider_code for e in RETAILER_MATRIX}
_PROVIDER_TO_SLUG: dict[str, str] = {v: k for k, v in _SLUG_TO_PROVIDER.items()}


def _adapter_key(slug: str) -> str:
    """The provider code for a chain, from the matrix (``mercadona`` -> ``apify-mercadona``)."""
    return _SLUG_TO_PROVIDER.get(slug, f"parsebot-{slug}")


def _counts(db: Session) -> dict[str, int]:
    return {
        "retailers": int(db.scalar(select(func.count()).select_from(Retailer)) or 0),
        "products": int(db.scalar(select(func.count()).select_from(Product)) or 0),
        "product_prices": int(db.scalar(select(func.count()).select_from(ProductPrice)) or 0),
    }


def bootstrap(db: Session, slugs: list[str]) -> list[str]:
    """Get-or-create a canonical Retailer per authorized slug. Returns the slugs created."""
    created: list[str] = []
    for slug in slugs:
        if slug not in AUTHORIZED_CHAINS:
            raise ValueError(f"chain {slug!r} is not authorized: {sorted(AUTHORIZED_CHAINS)}")
        existing = db.execute(
            select(Retailer).where(Retailer.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            continue  # fill-only: never overwrite an existing (possibly admin-edited) retailer
        db.add(
            Retailer(
                slug=slug,
                name=AUTHORIZED_CHAINS[slug],
                adapter_key=_adapter_key(slug),
                country="ES",
                is_synthetic=False,
            )
        )
        created.append(slug)
    db.flush()
    return created


def _resolve_slugs(*, all_chains: bool, provider: str | None) -> list[str]:
    if all_chains:
        return sorted(AUTHORIZED_CHAINS)
    if provider is not None:
        slug = _PROVIDER_TO_SLUG.get(provider)
        if slug is None or slug not in AUTHORIZED_CHAINS:
            raise ValueError(f"provider {provider!r} is not an authorized chain provider")
        return [slug]
    raise ValueError("pass --all or --provider <code>")


def run(*, all_chains: bool, provider: str | None, apply: bool) -> dict[str, object]:
    slugs = _resolve_slugs(all_chains=all_chains, provider=provider)
    with SessionLocal() as db:
        before = _counts(db)
        created = bootstrap(db, slugs)
        after = _counts(db)
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
        "requested": slugs,
        "created": created,
        "before": before,
        "after": after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="bootstrap all authorized chains")
    scope.add_argument("--provider", default=None, help="bootstrap one chain by provider code")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = run(all_chains=bool(args.all), provider=args.provider, apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
