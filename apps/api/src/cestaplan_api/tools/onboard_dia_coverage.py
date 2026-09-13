"""Idempotent DIA price-coverage onboarding for a set of canonical ingredients.

Given a list of ingredient ``canonical_name`` values, this tool searches DIA's public API (via the
already-built direct connector :class:`DiaClient` + :class:`DiaMapper`), picks the BEST plain/staple
product per ingredient, and *get-or-creates* the productive rows the LIVE plan rail reads to cost a
recipe by quantity:

  * :class:`Product`               — the DIA catalogue article (retailer ``dia``);
  * :class:`ProductPrice`          — a productive (``is_synthetic=False``) price observation, with
                                     ``package_quantity``/``package_unit`` taken from the parsed net
                                     content so the plan engine scales the cost by recipe quantity;
  * :class:`IngredientProductMapping` — the active ingredient->product link that turns the product
                                     into coverage (allow-list) for that ingredient.

Product selection (never maps junk):
  * the DIA ``display_name`` must CONTAIN every meaningful token of the ingredient term as WHOLE
    words (accent-insensitive), and must NOT be a prepared/flavored/derivative product (names with
    ``snack``/``salsa``/``crema``/``precocinad``/``frito``/``batido``/... are de-prioritized out);
  * among the survivors the cheapest plain staple with usable net content wins;
  * if nothing clears the relevance bar the ingredient is SKIPPED and reported — never mapped to a
    wrong or junk product.

``conversion_factor`` (see :func:`_conversion_factor`) is derived from the parsed net content so the
live costing scales correctly; it is ``None`` (the crude fallback) only when the net content is not
usable.

Modes::

    python -m cestaplan_api.tools.onboard_dia_coverage [--ingredients a,b] [--file names.txt]
    python -m cestaplan_api.tools.onboard_dia_coverage --commit

``--dry-run`` (default) runs the FULL logic — DIA search included (a read; only DB WRITES are gated)
— inside a transaction that ALWAYS rolls back, then prints a per-ingredient JSON summary.
``--commit`` commits a single transaction. Politeness is inherited from :class:`DiaClient` (built-in
rate limit); this tool adds no parallelism.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.contracts import ExternalCatalogProduct
from cestaplan_api.ingestion.providers.dia.client import DiaClient
from cestaplan_api.ingestion.providers.dia.mapping import DiaMapper
from cestaplan_api.models import (
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
    Store,
)
from cestaplan_api.services.importer import _match_ingredient
from cestaplan_api.services.recipe_costing import to_base
from cestaplan_api.services.store_zone_resolution import resolve_store_for_postal

# The DIA retailer + productive provenance stamped on every row this tool creates. ``source_type``
# is the valid enum for DIA's owner-authorized direct access (rights.py: parsebot-dia ->
# "authorized_partner"); ``source_name`` mirrors the provider display name.
RETAILER_SLUG = "dia"
SOURCE_TYPE = "authorized_partner"
SOURCE_NAME = "DIA (API pública directa)"
MATCH_METHOD = "dia_search_curated"
DEFAULT_MAX_PRODUCTS = 20
_PRICE_TTL_DAYS = 30

# canonical_name -> human DIA search term for names whose plain "_"->" " expansion mismatches the
# way DIA labels the staple. Everything not listed falls back to canonical_name.replace("_", " ").
ALIAS_TERMS: dict[str, str] = {
    "pollo_pechuga": "pechuga de pollo",
    "pollo_muslo": "muslo de pollo",
    "pasta_macarrones": "macarrones",
    "pasta_espagueti": "espaguetis",
    "garbanzos_cocido": "garbanzos cocidos",
    "maiz_dulce": "maíz dulce",
    "leche_entera": "leche entera",
    "leche_desnatada": "leche desnatada",
    "tomate_triturado": "tomate triturado",
    "avena_copos": "copos de avena",
    "aceite_oliva": "aceite de oliva",
    "arandano": "arándanos",
    "platano": "plátano",
    "yogur_natural": "yogur natural",
}

# Tokens dropped from a term before whole-word matching (connectives carry no discriminative value).
_STOPWORDS = frozenset({"de", "del", "con", "al", "a", "la", "el", "los", "las", "y", "en"})
# Stems that mark a prepared/flavored/derivative product (NOT the plain staple). Matched as
# SUBSTRINGS (so "precocinad" catches precocinado/precocinada) and skipped when they are genuinely
# part of the ingredient term itself.
_JUNK_STEMS: tuple[str, ...] = (
    "snack", "aperitivo", "salsa", "crema", "precocinad", "rebozad", "bravas", "frito", "fritas",
    "batido", "bebida", "galleta", "barrita", "papilla", "chips",
)
# Name tokens that carry no product identity (size/pack noise, bare unit words + the chain's own
# brand word), dropped when measuring how "plain" a name is (specificity -> confidence).
_NOISE_TOKENS = frozenset({
    "pack", "x", "dia", "l", "ml", "g", "gr", "kg", "cl", "unit", "ud", "uds", "unidad", "unidades",
})
_SIZE_TOKEN_RE = re.compile(r"^\d+(?:[.,]\d+)?(?:l|ml|g|gr|kg|cl)?$")


class DiaCoverageError(RuntimeError):
    """A fail-closed onboarding failure carrying a stable, sanitized ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class SearchClient(Protocol):
    """The single DIA capability this tool needs: a paginated text search (matches DiaClient)."""

    def search(self, term: str, *, max_products: int | None = None) -> list[dict[str, Any]]: ...


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def build_search_term(canonical_name: str) -> str:
    """Map a ``canonical_name`` to the human term searched on DIA (alias map, else "_"->" ")."""
    name = canonical_name.strip()
    return ALIAS_TERMS.get(name, name.replace("_", " "))


def _normalize(text: str) -> str:
    """Lowercase + strip accents so "maíz"/"maiz" and "plátano"/"platano" compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stem(token: str) -> str:
    """Strip a Spanish plural suffix ("es"/"s") so "patata"~"patatas", "macarrones"~"macarron"."""
    for suffix in ("es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _has_word(haystack_norm: str, word_norm: str) -> bool:
    """True when ``word_norm`` occurs as a WHOLE word in the haystack, tolerating a plural suffix
    on either side (so the singular term "tomate" matches the product "Tomates")."""
    pattern = rf"\b{re.escape(_stem(word_norm))}(?:es|s)?\b"
    return re.search(pattern, haystack_norm) is not None


def _junk_hit(name_norm: str, term_norm: str) -> str | None:
    """First prepared/flavored stem present in the name but NOT part of the term (else None)."""
    for stem in _JUNK_STEMS:
        if stem in term_norm:  # legitimately part of the ingredient (e.g. a term with "crema")
            continue
        if stem in name_norm:
            return stem
    return None


def _meaningful_tokens(name_norm: str) -> list[str]:
    """Name tokens that carry product identity (drop stopwords, size/pack noise and the brand)."""
    out: list[str] = []
    for tok in name_norm.split():
        if tok in _STOPWORDS or tok in _NOISE_TOKENS or _SIZE_TOKEN_RE.match(tok):
            continue
        out.append(tok)
    return out


@dataclass(slots=True)
class Scored:
    """A ranked DIA candidate for one ingredient term."""

    product: ExternalCatalogProduct
    eligible: bool
    relevance: float  # specificity in (0,1]: how plainly the name IS the ingredient
    junk_stem: str | None
    price_proxy: Decimal  # comparable €/base-unit (lower is cheaper), for ranking
    has_net_content: bool

    @property
    def sort_key(self) -> tuple[int, Decimal, int, str]:
        # Prefer usable net content, then cheapest, then the plainer (shorter) name, then stable id.
        return (
            0 if self.has_net_content else 1,
            self.price_proxy,
            len(self.product.product_name),
            self.product.external_product_id,
        )


def _net_base(product: ExternalCatalogProduct) -> Decimal | None:
    """Package net content expressed in its dimension's canonical base unit (g/ml/unit), or None."""
    if product.net_content_quantity is None or product.net_content_unit is None:
        return None
    based = to_base(product.net_content_quantity, product.net_content_unit.value)
    if based is None or based[0] <= 0:
        return None
    return based[0]


def _price_proxy(product: ExternalCatalogProduct) -> Decimal:
    """A comparable per-base-unit price for ranking: unit_price, else regular/net, else regular."""
    if product.unit_price is not None and product.unit_price > 0:
        return product.unit_price
    net = _net_base(product)
    if net is not None:
        return product.regular_price / net
    return product.regular_price


def _score(term: str, product: ExternalCatalogProduct) -> Scored:
    """Score one candidate against the term. Eligible = every core token present as a whole word AND
    not a prepared/flavored product."""
    term_norm = _normalize(term)
    name_norm = _normalize(product.product_name)
    core = [tok for tok in term_norm.split() if tok not in _STOPWORDS]
    all_present = bool(core) and all(_has_word(name_norm, tok) for tok in core)
    junk = _junk_hit(name_norm, term_norm)
    meaningful = _meaningful_tokens(name_norm)
    # Specificity: 1.0 when the name is exactly the ingredient; lower as extra descriptors pile on.
    relevance = len(core) / len(meaningful) if meaningful else 0.0
    relevance = min(1.0, relevance)
    return Scored(
        product=product,
        eligible=all_present and junk is None,
        relevance=relevance,
        junk_stem=junk,
        price_proxy=_price_proxy(product),
        has_net_content=_net_base(product) is not None,
    )


def choose_product(term: str, products: Sequence[ExternalCatalogProduct]) -> Scored | None:
    """Pick the best plain staple for ``term``, or None when nothing clears the relevance bar."""
    eligible = [s for s in (_score(term, p) for p in products) if s.eligible]
    if not eligible:
        return None
    return min(eligible, key=lambda s: s.sort_key)


def _confidence(relevance: float) -> Decimal:
    """Machine confidence derived from relevance (name plainness): 0.60..0.90."""
    value = Decimal("0.60") + Decimal("0.30") * _d(round(relevance, 4))
    return min(Decimal("0.90"), value).quantize(Decimal("0.01"))


def _conversion_factor(product: ExternalCatalogProduct) -> Decimal | None:
    """Factor that lets the LIVE plan rail scale cost by recipe quantity.

    It is the product PACKAGE size in the ingredient's canonical base unit (g, ml or unit)::

        conversion_factor = net_content_quantity * _TO_BASE[net_content_unit]

    computed via :func:`cestaplan_api.services.recipe_costing.to_base` (the same ``_TO_BASE`` the
    costing engine uses: kg->1000 g, l->1000 ml, g/ml/unit->1). So a "6 x 1 L" milk pack yields
    ``6 * 1000 = 6000`` (ml) and the engine buys ``packages = ceil(required_base_qty / 6000)``.

    Returns ``None`` (the crude fallback matching legacy rows) when the net content is absent or its
    unit is not a known mass/volume/count unit — i.e. when it cannot be turned into a base amount.
    """
    return _net_base(product)


@dataclass(slots=True)
class IngredientOutcome:
    canonical_name: str
    term: str
    status: str  # "mapped" | "reused" | "skipped"
    reason: str | None = None
    external_id: str | None = None
    product_name: str | None = None
    amount: Decimal | None = None
    unit_price: Decimal | None = None
    package_quantity: Decimal | None = None
    package_unit: str | None = None
    conversion_factor: Decimal | None = None
    confidence: Decimal | None = None
    product_created: bool = False
    price_created: bool = False
    mapping_created: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            out[key] = str(value) if isinstance(value, Decimal) else value
        return out


@dataclass(slots=True)
class OnboardDiff:
    products_created: int = 0
    prices_created: int = 0
    mappings_created: int = 0
    per_ingredient: list[IngredientOutcome] = field(default_factory=list)

    @property
    def mapped(self) -> int:
        return sum(1 for o in self.per_ingredient if o.status == "mapped")

    @property
    def reused(self) -> int:
        return sum(1 for o in self.per_ingredient if o.status == "reused")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.per_ingredient if o.status == "skipped")

    def as_dict(self) -> dict[str, Any]:
        return {
            "products_created": self.products_created,
            "prices_created": self.prices_created,
            "mappings_created": self.mappings_created,
            "mapped": self.mapped,
            "reused": self.reused,
            "skipped": self.skipped,
            "per_ingredient": [o.as_dict() for o in self.per_ingredient],
        }


def _get_or_create_product(
    session: Session, *, retailer_id: int, product: ExternalCatalogProduct,
    package_quantity: Decimal, package_unit: str,
) -> tuple[Product, bool]:
    """Get-or-create a :class:`Product` by (retailer, external_id). Never overwrites an existing."""
    existing = session.execute(
        select(Product).where(
            Product.retailer_id == retailer_id,
            Product.external_id == product.external_product_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = Product(
        retailer_id=retailer_id,
        external_id=product.external_product_id,
        name=product.product_name,
        brand=product.brand or None,
        package_quantity=package_quantity,
        package_unit=package_unit,
        category_code=None,
        image_url=product.image_url or None,
        is_synthetic=False,
    )
    session.add(row)
    session.flush()
    return row, True


def _ensure_price(
    session: Session, *, retailer_id: int, store_id: int, product: Product,
    mapped: ExternalCatalogProduct, package_quantity: Decimal, package_unit: str,
    confidence: Decimal, now: datetime, observed_at: datetime,
) -> bool:
    """Get-or-create the productive :class:`ProductPrice`. Idempotent by (retailer, store, product):
    a productive row already present is refreshed by reuse (never blindly appended)."""
    exists = session.execute(
        select(func.count()).select_from(ProductPrice).where(
            ProductPrice.retailer_id == retailer_id,
            ProductPrice.store_id == store_id,
            ProductPrice.product_id == product.id,
            ProductPrice.is_synthetic.is_(False),
        )
    ).scalar_one()
    if exists:
        return False
    if not mapped.regular_price > 0:  # defence-in-depth (the mapper already refuses <= 0)
        raise DiaCoverageError("dia_price_not_positive", product.external_id or "")
    session.add(ProductPrice(
        retailer_id=retailer_id,
        store_id=store_id,
        product_id=product.id,
        amount=mapped.regular_price,
        currency=mapped.currency or "EUR",
        package_quantity=package_quantity,
        package_unit=package_unit,
        unit_price=mapped.unit_price,
        promotion=None,
        availability="in_stock",
        source_type=SOURCE_TYPE,
        source_name=SOURCE_NAME,
        source_url=mapped.product_url or None,
        observed_at=observed_at,
        imported_at=now,
        expires_at=now + timedelta(days=_PRICE_TTL_DAYS),
        confidence_score=confidence,
        import_id=None,
        verification_status="machine_verified",
        is_synthetic=False,
    ))
    return True


def _ensure_mapping(
    session: Session, *, ingredient_id: int, product: Product, retailer_id: int,
    conversion_factor: Decimal | None, confidence: Decimal,
) -> bool:
    """Get-or-create the active ingredient->product mapping. Idempotent by an existing ACTIVE
    mapping for (ingredient, product, retailer)."""
    exists = session.execute(
        select(func.count()).select_from(IngredientProductMapping).where(
            IngredientProductMapping.ingredient_id == ingredient_id,
            IngredientProductMapping.product_id == product.id,
            IngredientProductMapping.retailer_id == retailer_id,
            IngredientProductMapping.is_active.is_(True),
        )
    ).scalar_one()
    if exists:
        return False
    session.add(IngredientProductMapping(
        ingredient_id=ingredient_id,
        product_id=product.id,
        retailer_id=retailer_id,
        conversion_factor=conversion_factor,
        preference_rank=0,
        confidence_score=confidence,
        match_method=MATCH_METHOD,
        verification_status="machine_verified",
        is_active=True,
    ))
    return True


def _package_fields(
    chosen: ExternalCatalogProduct,
) -> tuple[Decimal, str, Decimal | None] | None:
    """Resolve (package_quantity, package_unit, conversion_factor) for the chosen product.

    Prefers the parsed net content (fixed-package costing). Falls back to a nominal 1-unit package
    keyed on the ``unit_price_unit`` with ``conversion_factor=None`` (the crude legacy fallback)
    when net content is not usable. Returns ``None`` when neither is available (nothing to cost).
    """
    net = _net_base(chosen)
    if net is not None and chosen.net_content_quantity is not None and chosen.net_content_unit:
        return chosen.net_content_quantity, chosen.net_content_unit.value, net
    if chosen.unit_price is not None and chosen.unit_price > 0 and chosen.unit_price_unit:
        return Decimal("1"), chosen.unit_price_unit, None
    return None


def onboard(
    session: Session,
    *,
    client: SearchClient,
    ingredient_names: Sequence[str],
    max_products: int = DEFAULT_MAX_PRODUCTS,
    national_postal_code: str = "28041",
    mapper: DiaMapper | None = None,
    now: datetime | None = None,
) -> OnboardDiff:
    """Onboard DIA coverage for ``ingredient_names`` additively. Caller controls commit/rollback."""
    now = now or datetime.now(UTC)
    mapper = mapper or DiaMapper()
    diff = OnboardDiff()

    retailer = session.execute(
        select(Retailer).where(Retailer.slug == RETAILER_SLUG)
    ).scalar_one_or_none()
    if retailer is None:
        raise DiaCoverageError("retailer_not_found", RETAILER_SLUG)
    # National scope: DIA's default warehouse zone (a single external-code-less store), which is
    # get-or-created idempotently. The live rail aggregates ProductPrice across all of a chain's
    # stores, so the specific store never skews costing — it only satisfies the NOT NULL store_id.
    store: Store = resolve_store_for_postal(session, retailer.id, national_postal_code)

    seen: set[str] = set()
    for raw_name in ingredient_names:
        canonical = raw_name.strip()
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        diff.per_ingredient.append(
            _onboard_one(
                session, client=client, mapper=mapper, canonical=canonical,
                retailer_id=retailer.id, store_id=store.id, max_products=max_products,
                now=now, diff=diff,
            )
        )
    session.flush()
    return diff


def _onboard_one(
    session: Session, *, client: SearchClient, mapper: DiaMapper, canonical: str,
    retailer_id: int, store_id: int, max_products: int, now: datetime, diff: OnboardDiff,
) -> IngredientOutcome:
    term = build_search_term(canonical)
    outcome = IngredientOutcome(canonical_name=canonical, term=term, status="skipped")

    ingredient = _match_ingredient(session, canonical)
    if ingredient is None:
        outcome.reason = "ingrediente canónico desconocido"
        return outcome

    records = client.search(term, max_products=max_products)
    products = mapper.map_products(records, observed_at=now)
    chosen = choose_product(term, products)
    if chosen is None:
        outcome.reason = (
            "sin candidato relevante (ninguno contiene el término como palabras completas / "
            "solo productos preparados)"
        )
        return outcome

    package = _package_fields(chosen.product)
    if package is None:
        outcome.reason = "producto sin contenido neto ni precio unitario utilizable"
        return outcome
    package_quantity, package_unit, conversion_factor = package
    confidence = _confidence(chosen.relevance)

    _fill_chosen(outcome, chosen, package_quantity, package_unit, conversion_factor, confidence)

    product, product_created = _get_or_create_product(
        session, retailer_id=retailer_id, product=chosen.product,
        package_quantity=package_quantity, package_unit=package_unit,
    )
    price_created = _ensure_price(
        session, retailer_id=retailer_id, store_id=store_id, product=product,
        mapped=chosen.product, package_quantity=package_quantity, package_unit=package_unit,
        confidence=confidence, now=now, observed_at=chosen.product.observed_at or now,
    )
    mapping_created = _ensure_mapping(
        session, ingredient_id=ingredient.id, product=product, retailer_id=retailer_id,
        conversion_factor=conversion_factor, confidence=confidence,
    )

    diff.products_created += int(product_created)
    diff.prices_created += int(price_created)
    diff.mappings_created += int(mapping_created)
    outcome.product_created = product_created
    outcome.price_created = price_created
    outcome.mapping_created = mapping_created
    # A run where nothing was created for an already-onboarded ingredient is a clean no-op (reused).
    outcome.status = "mapped" if (product_created or price_created or mapping_created) else "reused"
    outcome.reason = None
    return outcome


def _fill_chosen(
    outcome: IngredientOutcome, chosen: Scored, package_quantity: Decimal, package_unit: str,
    conversion_factor: Decimal | None, confidence: Decimal,
) -> None:
    outcome.external_id = chosen.product.external_product_id
    outcome.product_name = chosen.product.product_name
    outcome.amount = chosen.product.regular_price
    outcome.unit_price = chosen.product.unit_price
    outcome.package_quantity = package_quantity
    outcome.package_unit = package_unit
    outcome.conversion_factor = conversion_factor
    outcome.confidence = confidence


def default_ingredient_names(session: Session) -> list[str]:
    """Distinct MANDATORY canonical_names referenced by published AI recipes on the live rail.

    origin='ai_generated' AND is_public AND NOT is_synthetic AND household_id IS NULL AND not
    deleted, mandatory (``optional=False``) lines only. Ordered for a stable, deterministic run.
    """
    from cestaplan_api.models import Recipe, RecipeIngredient

    rows = session.execute(
        select(RecipeIngredient.canonical_name)
        .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
        .where(
            Recipe.origin == "ai_generated",
            Recipe.is_public.is_(True),
            Recipe.is_synthetic.is_(False),
            Recipe.household_id.is_(None),
            Recipe.deleted_at.is_(None),
            RecipeIngredient.optional.is_(False),
        )
        .distinct()
        .order_by(RecipeIngredient.canonical_name)
    ).scalars().all()
    return list(rows)


def _read_name_file(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiaCoverageError("name_file_unreadable", str(path)) from exc
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _build_client(settings: Settings) -> DiaClient:
    """Construct the direct DIA client from settings (same footing as :class:`DiaProvider`)."""
    return DiaClient(
        user_agent=settings.dia_user_agent,  # DIA browser UA (owner-authorized; see config)
        contact_email=settings.scraping_contact_email,
        timeout=float(settings.scraping_timeout_seconds),
        max_retries=settings.scraping_max_retries,
        delay_bounds_seconds=settings.scraping_request_delay_bounds_seconds,
    )


def _resolve_names(
    session: Session, *, ingredients: str | None, file: Path | None,
) -> list[str]:
    if ingredients:
        return [name.strip() for name in ingredients.split(",") if name.strip()]
    if file is not None:
        return _read_name_file(file)
    return default_ingredient_names(session)


def run(
    *,
    ingredients: str | None = None,
    file: Path | None = None,
    commit: bool = False,
    max_products: int = DEFAULT_MAX_PRODUCTS,
    settings: Settings | None = None,
    client: SearchClient | None = None,
) -> dict[str, Any]:
    """Execute onboarding in ONE transaction. dry-run rolls back; commit persists. DIA search (a
    read) always runs; only DB writes are gated by ``commit``."""
    settings = settings or get_settings()
    session = SessionLocal()
    owns_client = client is None
    search_client: SearchClient = client or _build_client(settings)
    try:
        names = _resolve_names(session, ingredients=ingredients, file=file)
        diff = onboard(
            session, client=search_client, ingredient_names=names, max_products=max_products,
            national_postal_code=settings.dia_postal_code,
        )
        result: dict[str, Any] = {
            "mode": "commit" if commit else "dry-run",
            "retailer": RETAILER_SLUG,
            "ingredient_count": len(names),
            "diff": diff.as_dict(),
        }
        if commit:
            session.commit()
            result["committed"] = True
        else:
            session.rollback()
            result["committed"] = False
        return result
    except DiaCoverageError as exc:
        session.rollback()
        return {
            "mode": "commit" if commit else "dry-run", "retailer": RETAILER_SLUG,
            "committed": False, "error": exc.code, "detail": exc.detail,
        }
    finally:
        session.close()
        if owns_client and isinstance(search_client, DiaClient):
            search_client.close()


def _print_summary(result: dict[str, Any]) -> None:
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ingredients", help="comma-separated canonical_names to onboard")
    parser.add_argument("--file", type=Path, help="file with one canonical_name per line")
    parser.add_argument(
        "--max-products", type=int, default=DEFAULT_MAX_PRODUCTS,
        help=f"max DIA products fetched per ingredient (default {DEFAULT_MAX_PRODUCTS})",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="run + rollback (default)")
    mode.add_argument("--commit", action="store_true", help="run + commit in one transaction")
    args = parser.parse_args(argv)
    result = run(
        ingredients=args.ingredients, file=args.file, commit=bool(args.commit),
        max_products=args.max_products,
    )
    _print_summary(result)
    return 0 if result.get("error") is None else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
