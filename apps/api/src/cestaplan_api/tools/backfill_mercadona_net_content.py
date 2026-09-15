"""Idempotent backfill of REAL net content into productive Mercadona ``ProductPrice`` rows.

Why this tool exists
--------------------
The LIVE plan rail costs a recipe line through the ``cestaplan_engine`` package. The chain is:

    planning_context._build_catalog / _package_option (services/planning_context.py:337,395)
        -> PackageOptionDTO(package_quantity=..., package_unit=...) taken from ProductPrice
    -> cestaplan_engine.provisioning.Provisioner.provision (provisioning.py:83)
        target_unit  = product.packages[0].package_unit           (provisioning.py:78)
        pending      = UnitConverter.convert(recipe_qty, recipe_unit, target_unit)  (:112)
    -> cestaplan_engine.packaging.PackageOptimizer.choose -> compute_packages  (packaging.py:122)
        packages   = ceil(pending / package_quantity)             (packaging.py:18,57)
        total_cost = packages * package_price                     (packaging.py:62)

So the engine scales cost by recipe quantity via
``ceil(required_in_package_unit / package_quantity) * amount`` AFTER converting the recipe quantity
into ``package_unit`` (g<->kg, ml<->l). The per-serving money the UI shows
(``services/shopping_semantics.line_cost_breakdown``) is ``total_cost * used / purchased``, i.e. the
fraction of the package actually consumed — which is only correct when ``package_quantity`` /
``package_unit`` carry the product's REAL net content. The mapping's ``conversion_factor`` is NOT
read by this rail (grep: only written by the DIA tool / demo seed), so it is left untouched.

The bug this fixes
------------------
The projector that writes ``ProductPrice`` from observations reads ``variant.package_quantity`` /
``variant.package_unit`` (ingestion/current_price.py:183-186), defaulting to ``1`` / ``"unit"`` when
they are null. Mercadona ingestion populated the variant's ``net_content_quantity`` /
``net_content_unit`` (the mapper's structured output) but left ``package_quantity`` /
``package_unit`` null, so every productive Mercadona ``ProductPrice`` landed with
``package_quantity=1``, ``package_unit='unit'``. A recipe line measured in g/ml then fails to
convert into ``'unit'`` (or, when the recipe unit is itself counted, buys a whole package per unit):
the line is mis-costed instead of scaled by quantity. DIA rows were onboarded WITH real net content
and are already correct.

What this tool does
-------------------
For every active Mercadona ``ingredient_product_mapping`` whose product still has a productive
(``is_synthetic=False``) ``ProductPrice`` with ``package_quantity in (NULL, 1)`` and
``package_unit in ('unit', NULL)``, it looks up the product's real net content and UPDATES that
price's ``package_quantity`` + ``package_unit`` to it (e.g. a 1 L oil -> ``1`` / ``'l'``). The net
content is obtained from the SAME structured parser the live ingestion uses
(:class:`ApifyMercadonaMapper`, via :class:`MercadonaProvider`, keyed by the Mercadona product id =
``Product.external_id``); it is NEVER guessed from the product name. When the net content cannot be
determined reliably (product absent from the catalogue, or the mapper dropped it: an approximate
size, a pack whose ``unit_size``/``reference_price`` are inconsistent, or a counted/unknown unit)
the product is SKIPPED and reported, left exactly as-is.

DIA rows, synthetic rows, ``Product`` and ``ingredient_product_mapping`` are never touched.

Idempotency
-----------
The mapper only ever emits mass/volume net-content units (g/kg/ml/l), so a backfilled row's
``package_unit`` is no longer ``'unit'`` and the stale-row filter never re-selects it. A second run
is therefore a clean no-op.

Modes::

    python -m cestaplan_api.tools.backfill_mercadona_net_content            # dry-run (rolls back)
    python -m cestaplan_api.tools.backfill_mercadona_net_content --commit   # commit in one txn

``--dry-run`` (default) builds the net-content index (a courteous full crawl via the direct
connector's built-in rate limit) and runs the FULL logic inside a transaction that ALWAYS rolls
back, printing a per-product before/after JSON summary. ``--commit`` persists in a single
transaction. This tool adds no parallelism.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct, ProductQuery
from cestaplan_api.ingestion.providers.mercadona.provider import MercadonaProvider
from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
)

RETAILER_SLUG = "mercadona"
# Units the engine can scale a recipe quantity into (mass/volume). The mapper only ever emits these
# for net content; a counted/unknown unit means "net content not usable" -> the product is skipped.
_SCALABLE_UNITS = frozenset({"g", "kg", "ml", "l"})

# external_product_id -> (net_content_quantity, net_content_unit). The net-content source.
NetContentIndex = Mapping[str, tuple[Decimal, str]]


class CatalogSource(Protocol):
    """The single capability the crawl path needs: bounded product iteration (MercadonaProvider)."""

    def iterate_products(self, query: ProductQuery) -> Iterable[ExternalCatalogProduct]: ...


class BackfillError(RuntimeError):
    """A fail-closed backfill failure carrying a stable, sanitized ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(slots=True)
class ProductOutcome:
    """Per-product before/after record for the summary table."""

    external_id: str | None
    product_id: int
    product_name: str
    ingredients: list[str]
    status: str  # "updated" | "skipped"
    reason: str | None = None
    old_package_quantity: Decimal | None = None
    old_package_unit: str | None = None
    new_package_quantity: Decimal | None = None
    new_package_unit: str | None = None
    rows_updated: int = 0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            out[key] = str(value) if isinstance(value, Decimal) else value
        return out


@dataclass(slots=True)
class BackfillDiff:
    per_product: list[ProductOutcome] = field(default_factory=list)

    @property
    def rows_updated(self) -> int:
        return sum(o.rows_updated for o in self.per_product)

    @property
    def products_updated(self) -> int:
        return sum(1 for o in self.per_product if o.status == "updated")

    @property
    def products_skipped(self) -> int:
        return sum(1 for o in self.per_product if o.status == "skipped")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_updated": self.rows_updated,
            "products_updated": self.products_updated,
            "products_skipped": self.products_skipped,
            "per_product": [o.as_dict() for o in self.per_product],
        }


def _is_scalable_unit(unit: str | None) -> bool:
    return bool(unit) and unit.strip().lower() in _SCALABLE_UNITS


def _mapped_products(session: Session, retailer_id: int) -> list[tuple[Product, list[str]]]:
    """Distinct Mercadona products behind an ACTIVE mapping, with their ingredient canonical names.

    Ordered by product id for a deterministic run; ingredient names are de-duplicated and sorted.
    """
    rows = session.execute(
        select(Product, Ingredient.canonical_name)
        .join(IngredientProductMapping, IngredientProductMapping.product_id == Product.id)
        .join(Ingredient, Ingredient.id == IngredientProductMapping.ingredient_id)
        .where(
            IngredientProductMapping.is_active.is_(True),
            Product.retailer_id == retailer_id,
            Product.deleted_at.is_(None),
        )
        .order_by(Product.id)
    ).all()

    by_product: dict[int, tuple[Product, set[str]]] = {}
    for product, canonical_name in rows:
        entry = by_product.setdefault(product.id, (product, set()))
        entry[1].add(canonical_name)
    return [(product, sorted(names)) for product, names in by_product.values()]


def _stale_price_stmt(product_id: int) -> Select[tuple[ProductPrice]]:
    """Productive prices of a product still carrying the placeholder net content (1 / 'unit')."""
    return select(ProductPrice).where(
        ProductPrice.product_id == product_id,
        ProductPrice.is_synthetic.is_(False),
        or_(ProductPrice.package_quantity.is_(None), ProductPrice.package_quantity == Decimal("1")),
        or_(ProductPrice.package_unit.is_(None), ProductPrice.package_unit == "unit"),
    )


def backfill(
    session: Session,
    *,
    net_content: NetContentIndex,
    now: datetime | None = None,
) -> BackfillDiff:
    """Backfill productive Mercadona ``ProductPrice`` net content. Caller controls commit/rollback.

    ``net_content`` maps a Mercadona product id (``Product.external_id``) to its real
    ``(net_content_quantity, net_content_unit)``. A product absent from the index, without an
    ``external_id``, or whose net-content unit is not a scalable mass/volume unit is SKIPPED and
    reported — never guessed.
    """
    now = now or datetime.now(UTC)
    diff = BackfillDiff()

    retailer = session.execute(
        select(Retailer).where(Retailer.slug == RETAILER_SLUG)
    ).scalar_one_or_none()
    if retailer is None:
        raise BackfillError("retailer_not_found", RETAILER_SLUG)

    for product, ingredient_names in _mapped_products(session, retailer.id):
        diff.per_product.append(
            _backfill_one(session, product, ingredient_names, net_content=net_content)
        )
    session.flush()
    return diff


def _backfill_one(
    session: Session,
    product: Product,
    ingredient_names: list[str],
    *,
    net_content: NetContentIndex,
) -> ProductOutcome:
    outcome = ProductOutcome(
        external_id=product.external_id,
        product_id=product.id,
        product_name=product.name,
        ingredients=ingredient_names,
        status="skipped",
    )

    stale = list(session.execute(_stale_price_stmt(product.id)).scalars().all())
    if not stale:
        outcome.reason = "sin precio productivo con contenido neto marcador (1/'unit')"
        return outcome

    outcome.old_package_quantity = stale[0].package_quantity
    outcome.old_package_unit = stale[0].package_unit

    if not product.external_id:
        outcome.reason = "producto sin external_id (id de Mercadona) para resolver contenido neto"
        return outcome

    resolved = net_content.get(product.external_id)
    if resolved is None:
        outcome.reason = "contenido neto no disponible en el catálogo (ausente o descartado)"
        return outcome

    net_qty, net_unit = resolved
    if net_qty <= 0 or not _is_scalable_unit(net_unit):
        outcome.reason = f"contenido neto no escalable ({net_qty} {net_unit!r}); no se inventa"
        return outcome

    unit = net_unit.strip().lower()
    for price in stale:
        price.package_quantity = net_qty
        price.package_unit = unit
        outcome.rows_updated += 1

    outcome.status = "updated"
    outcome.new_package_quantity = net_qty
    outcome.new_package_unit = unit
    outcome.reason = None
    return outcome


def build_net_content_index(
    *,
    settings: Settings | None = None,
    provider: CatalogSource | None = None,
    max_products: int | None = None,
) -> dict[str, tuple[Decimal, str]]:
    """Crawl the Mercadona catalogue once (direct connector) and index net content by product id.

    Reuses :class:`MercadonaProvider` (a courteous full crawl via :class:`MercadonaClient`'s
    built-in rate limit, mapped by :class:`ApifyMercadonaMapper`). Only products the mapper gives a
    trustworthy structured net content are indexed; the first non-null value per id wins.
    """
    settings = settings or get_settings()
    provider = provider or MercadonaProvider(settings=settings)
    index: dict[str, tuple[Decimal, str]] = {}
    for mapped in provider.iterate_products(ProductQuery(max_products=max_products)):
        if mapped.net_content_quantity is None or mapped.net_content_unit is None:
            continue
        index.setdefault(
            mapped.external_product_id,
            (mapped.net_content_quantity, mapped.net_content_unit.value),
        )
    return index


def run(
    *,
    commit: bool = False,
    settings: Settings | None = None,
    net_content: NetContentIndex | None = None,
    provider: CatalogSource | None = None,
    max_products: int | None = None,
) -> dict[str, Any]:
    """Execute the backfill in ONE transaction. dry-run rolls back; commit persists.

    The net-content index is built (a catalogue crawl, a read) even in dry-run — only DB writes are
    gated by ``commit``. ``net_content`` may be injected (tests) to skip the crawl entirely.
    """
    settings = settings or get_settings()
    session = SessionLocal()
    try:
        index = (
            net_content
            if net_content is not None
            else build_net_content_index(
                settings=settings, provider=provider, max_products=max_products
            )
        )
        diff = backfill(session, net_content=index)
        result: dict[str, Any] = {
            "mode": "commit" if commit else "dry-run",
            "retailer": RETAILER_SLUG,
            "catalog_net_content_products": len(index),
            "diff": diff.as_dict(),
        }
        if commit:
            session.commit()
            result["committed"] = True
        else:
            session.rollback()
            result["committed"] = False
        return result
    except BackfillError as exc:
        session.rollback()
        return {
            "mode": "commit" if commit else "dry-run",
            "retailer": RETAILER_SLUG,
            "committed": False,
            "error": exc.code,
            "detail": exc.detail,
        }
    finally:
        session.close()


def _print_summary(result: dict[str, Any]) -> None:
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="cap products crawled from Mercadona (default: full catalogue)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="run + rollback (default)")
    mode.add_argument("--commit", action="store_true", help="run + commit in one transaction")
    args = parser.parse_args(argv)
    result = run(commit=bool(args.commit), max_products=args.max_products)
    _print_summary(result)
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
