"""Idempotent Mercadona price-coverage onboarding from the ALREADY-CRAWLED catalogue.

Mercadona has no text-search endpoint, but its catalogue is already crawled into the DB
(:class:`Product` + :class:`ProductPrice` + :class:`ProductVariant`). This tool picks the best
plain/staple ALREADY-STORED product per canonical ingredient — reusing the DIA onboarder's proven
relevance + prepared/flavored ("junk") filtering (so "plátano" never maps to a smoothie, "naranja"
never to a soft drink) — and *get-or-creates* the active :class:`IngredientProductMapping` the LIVE
plan rail reads (see ``planning_context._build_catalog``). It creates NO products and NO prices —
those already exist from the monthly crawl; only the ingredient->product link was missing.

Product selection (never maps junk, fail-closed):
  * the product ``display_name`` must contain every core token of the ingredient term as WHOLE
    words (accent-insensitive) and must NOT be a prepared/flavored/derivative product;
  * among survivors the one with usable net content, then the cheapest per base unit, then the
    plainest (shortest) name wins;
  * if nothing clears the bar the ingredient is SKIPPED and reported — never mapped to junk.

Modes (mirrors onboard_dia_coverage)::

    python -m cestaplan_api.tools.onboard_mercadona_coverage [--ingredients a,b] [--file names.txt]
    python -m cestaplan_api.tools.onboard_mercadona_coverage --commit

``--dry-run`` (default) runs the full selection inside a transaction that ALWAYS rolls back, then
prints a per-ingredient JSON review. ``--commit`` persists in one transaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import IngredientProductMapping, Product, ProductVariant, Retailer
from cestaplan_api.services.importer import _match_ingredient
from cestaplan_api.services.planning_context import _latest_prices
from cestaplan_api.services.recipe_costing import to_base

# Reuse the DIA onboarder's PROVEN pure matching helpers (single source of truth for the junk list
# and whole-word/accent-insensitive matching), so both onboarders stay consistent.
from cestaplan_api.tools.onboard_dia_coverage import (
    _STOPWORDS,
    _confidence,
    _has_word,
    _junk_hit,
    _meaningful_tokens,
    _normalize,
    build_search_term,
)

RETAILER_SLUG = "mercadona"
MATCH_METHOD = "mercadona_catalog_curated"

# Prepared/flavored stems seen in Mercadona's catalogue that the DIA junk list doesn't cover
# (dairy desserts, smoothies, prepared dishes). Matched as substrings, term-aware (skipped when
# genuinely part of the ingredient term). Complements onboard_dia_coverage._JUNK_STEMS.
_EXTRA_JUNK: tuple[str, ...] = (
    "smoothie", "petit", "danonino", "actimel", "yopro", "griego", "azucarad",
    "sabor", "bombon", "flan", "gelatina", "tarta", "bizcocho", "ensalada", "salteado",
    "wok", "pizza", "sandwich", "sándwich", "relleno", "gratinad", "empanadilla",
)


def _extra_junk_hit(name_norm: str, term_norm: str) -> str | None:
    """First Mercadona-specific prepared/flavored stem in the name but NOT part of the term."""
    for stem in _EXTRA_JUNK:
        if stem in term_norm:
            continue
        if stem in name_norm:
            return stem
    return None


def _variant_net_base(v: ProductVariant) -> Decimal | None:
    """Variant net content in its canonical base unit (g/ml/unit), or None if not usable."""
    if v.net_content_quantity is None or not v.net_content_unit:
        return None
    based = to_base(v.net_content_quantity, v.net_content_unit)
    if based is None or based[0] <= 0:
        return None
    return based[0]


@dataclass(slots=True)
class VScored:
    variant: ProductVariant
    eligible: bool
    relevance: float
    junk_stem: str | None
    net_base: Decimal | None
    price: Decimal | None  # €/base-unit proxy for ranking (lower is cheaper)

    @property
    def sort_key(self) -> tuple[int, Decimal, int, int]:
        return (
            0 if self.net_base is not None else 1,
            self.price if self.price is not None else Decimal("999999"),
            len(self.variant.display_name),
            self.variant.id,
        )


def _score_variant(term: str, v: ProductVariant, amount: Decimal | None) -> VScored:
    term_norm = _normalize(term)
    name_norm = _normalize(v.display_name)
    core = [tok for tok in term_norm.split() if tok not in _STOPWORDS]
    all_present = bool(core) and all(_has_word(name_norm, tok) for tok in core)
    junk = _junk_hit(name_norm, term_norm) or _extra_junk_hit(name_norm, term_norm)
    meaningful = _meaningful_tokens(name_norm)
    relevance = min(1.0, len(core) / len(meaningful)) if meaningful else 0.0
    net_base = _variant_net_base(v)
    price_proxy: Decimal | None = None
    if v.unit_price is not None and v.unit_price > 0:
        price_proxy = v.unit_price
    elif amount is not None and net_base is not None:
        price_proxy = amount / net_base
    elif amount is not None:
        price_proxy = amount
    eligible = all_present and junk is None and v.product_id is not None
    return VScored(v, eligible, relevance, junk, net_base, price_proxy)


def _choose(
    term: str, variants: Sequence[ProductVariant], price_by_product: dict[int, Decimal]
) -> VScored | None:
    scored = [
        _score_variant(term, v, price_by_product.get(v.product_id) if v.product_id else None)
        for v in variants
    ]
    eligible = [s for s in scored if s.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda s: s.sort_key)


@dataclass(slots=True)
class Outcome:
    canonical_name: str
    term: str
    status: str  # "mapped" | "reused" | "skipped"
    reason: str | None = None
    product_id: int | None = None
    product_name: str | None = None
    price: Decimal | None = None
    net_base: Decimal | None = None
    confidence: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()
        }


@dataclass(slots=True)
class Diff:
    mappings_created: int = 0
    per_ingredient: list[Outcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        counts = {"mapped": 0, "reused": 0, "skipped": 0}
        for o in self.per_ingredient:
            counts[o.status] = counts.get(o.status, 0) + 1
        return {"mappings_created": self.mappings_created, **counts,
                "per_ingredient": [o.as_dict() for o in self.per_ingredient]}


def _has_active_mapping(db: Session, ingredient_id: int, retailer_id: int) -> bool:
    return db.execute(
        select(IngredientProductMapping.id).where(
            IngredientProductMapping.ingredient_id == ingredient_id,
            IngredientProductMapping.retailer_id == retailer_id,
            IngredientProductMapping.is_active.is_(True),
        ).limit(1)
    ).first() is not None


def onboard(db: Session, ingredient_names: Sequence[str]) -> Diff:
    """Map each ingredient to its best already-crawled Mercadona product. Caller owns the commit."""
    diff = Diff()
    retailer_id = db.execute(
        select(Retailer.id).where(Retailer.slug == RETAILER_SLUG)
    ).scalar_one()

    # Load the crawled catalogue once (accent-insensitive matching happens in Python).
    variants = db.execute(
        select(ProductVariant).where(
            ProductVariant.retailer_id == retailer_id,
            ProductVariant.product_id.is_not(None),
        )
    ).scalars().all()
    price_by_product = {pid: pp.amount for pid, pp in _latest_prices(db, retailer_id).items()}

    seen: set[str] = set()
    for raw in ingredient_names:
        canonical = raw.strip()
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        diff.per_ingredient.append(
            _onboard_one(db, canonical, retailer_id, variants, price_by_product, diff)
        )
    db.flush()
    return diff


def _onboard_one(
    db: Session, canonical: str, retailer_id: int,
    variants: Sequence[ProductVariant], price_by_product: dict[int, Decimal], diff: Diff,
) -> Outcome:
    term = build_search_term(canonical)
    out = Outcome(canonical_name=canonical, term=term, status="skipped")

    ingredient = _match_ingredient(db, canonical)
    if ingredient is None:
        out.reason = "ingrediente canónico desconocido"
        return out
    if _has_active_mapping(db, ingredient.id, retailer_id):
        out.status = "reused"
        out.reason = "ya tenía mapeo activo"
        return out

    chosen = _choose(term, variants, price_by_product)
    if chosen is None:
        out.reason = "sin producto plano relevante en el catálogo (solo preparados/derivados)"
        return out

    confidence = _confidence(chosen.relevance)
    product = db.get(Product, chosen.variant.product_id)
    out.product_id = chosen.variant.product_id
    out.product_name = product.name if product else chosen.variant.display_name
    out.price = price_by_product.get(chosen.variant.product_id)
    out.net_base = chosen.net_base
    out.confidence = confidence

    db.add(IngredientProductMapping(
        ingredient_id=ingredient.id,
        product_id=chosen.variant.product_id,
        product_variant_id=chosen.variant.id,
        retailer_id=retailer_id,
        conversion_factor=chosen.net_base,
        preference_rank=0,
        confidence_score=confidence,
        match_method=MATCH_METHOD,
        verification_status="machine_verified",
        is_active=True,
    ))
    diff.mappings_created += 1
    out.status = "mapped"
    out.reason = None
    return out


def _resolve_names(db: Session, ingredients: str | None, file: Path | None) -> list[str]:
    if ingredients:
        return [n.strip() for n in ingredients.split(",") if n.strip()]
    if file is not None:
        return [ln.strip() for ln in file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # Default: distinct canonical names used by breakfast/snack recipes that are not costable yet.
    from cestaplan_api.models import Recipe, RecipeIngredient

    return list(db.execute(
        select(RecipeIngredient.canonical_name)
        .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
        .where(Recipe.deleted_at.is_(None), RecipeIngredient.optional.is_(False))
        .distinct()
        .order_by(RecipeIngredient.canonical_name)
    ).scalars().all())


def run(
    *, ingredients: str | None = None, file: Path | None = None, commit: bool = False
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        names = _resolve_names(db, ingredients, file)
        diff = onboard(db, names)
        if commit:
            db.commit()
        else:
            db.rollback()
        return {
            "mode": "commit" if commit else "dry-run",
            "retailer": RETAILER_SLUG,
            "committed": commit,
            "ingredient_count": len(names),
            "diff": diff.as_dict(),
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Onboard Mercadona coverage from the crawled catalogue."
    )
    parser.add_argument("--ingredients", help="comma-separated canonical_names")
    parser.add_argument("--file", type=Path, help="file with one canonical_name per line")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="run + rollback (default)")
    group.add_argument("--commit", action="store_true", help="run + commit")
    args = parser.parse_args(argv)
    result = run(ingredients=args.ingredients, file=args.file, commit=bool(args.commit))
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
