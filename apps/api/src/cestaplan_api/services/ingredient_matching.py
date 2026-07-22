"""Map real chain-store :class:`Product`\\ s onto canonical :class:`Ingredient`\\ s.

Real products synced from Open Prices (OFF-enriched) carry real barcodes, names and
sometimes an OFF category, but **no** :class:`IngredientProductMapping` — so the planner
cannot cost recipes on those stores. This service builds that missing layer.

Design (deliberately CONSERVATIVE — a wrong mapping mis-costs a recipe, so we prefer to
leave a product unmapped over a doubtful match):

1. **OFF category → canonical ingredient.** A small curated map keyed on the product's OFF
   ``category_code`` (e.g. ``basmati-rices`` → ``arroz_basmati``). Highest confidence.
2. **Name token / keyword match.** The product name is normalised (lower-cased,
   accent-stripped, size/unit/filler tokens dropped) and checked against a curated
   Spanish synonym table of *required* tokens plus *forbidden* guard tokens (so
   ``TURRON … CACAHUETE`` never maps to peanuts, ``TOMATE FRITO`` never to fresh tomato).
3. **Unit sanity check.** A liquid ingredient (default unit ml/l) is never mapped to a
   product priced by mass (and vice versa).

Every candidate carries a confidence in ``0..1``; :func:`match_product` returns ``None``
below :data:`DEFAULT_MIN_CONFIDENCE`. Nothing is ever guessed and no price is read/written.

The service flushes but does not commit; the command / admin endpoint owns the transaction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    Ingredient,
    IngredientProductMapping,
    Product,
    ProductPrice,
    Retailer,
)

#: Minimum confidence for a mapping to be accepted (documented threshold). Every curated
#: rule below scores at or above this; ambiguous rules sit just above it so a small nudge
#: downward would drop them first.
DEFAULT_MIN_CONFIDENCE = Decimal("0.70")

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
# Pure size / packaging / measurement tokens carry no ingredient signal.
_UNIT_TOKENS = frozenset(
    {
        "g", "gr", "grs", "gramo", "gramos", "kg", "kgs", "mg",
        "ml", "cl", "l", "litro", "litros", "lt",
        "ud", "uds", "u", "uni", "unid", "unidad", "unidades", "pack", "packs",
        "x", "cm", "und",
    }
)
# Marketing filler that never disambiguates an ingredient.
_FILLER_TOKENS = frozenset(
    {"bio", "eco", "ecologico", "ecologica", "pn", "extra", "primera"}
)


def _normalize_tokens(text: str) -> frozenset[str]:
    """Lower-case, strip accents, split on non-alphanumerics, drop size/filler/number tokens."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    raw = re.split(r"[^a-z0-9]+", ascii_text)
    return frozenset(
        tok
        for tok in raw
        if tok
        and not tok.isdigit()
        and tok not in _UNIT_TOKENS
        and tok not in _FILLER_TOKENS
    )


def _has_required(token: str, tokens: frozenset[str]) -> bool:
    """A required token matches on exact equality, or (for stems ≥4 chars) as a prefix.

    Prefix matching absorbs Spanish plurals (``aceituna`` → ``aceitunas``) while short
    tokens (``sal``, ``ajo``) stay exact so they never match *inside* another word
    (``sal`` must not fire on ``salsa`` / ``salmon``).
    """
    if token in tokens:
        return True
    if len(token) >= 4:
        return any(t.startswith(token) for t in tokens)
    return False


# --------------------------------------------------------------------------- #
# OFF category → canonical ingredient (curated, high confidence)
# --------------------------------------------------------------------------- #
# Only categories whose mapping to one of the 75 canonical ingredients is unambiguous.
# Deliberately omitted (form/kind ambiguous → would mis-cost): ``chickens`` (whole bird,
# cut unknown), ``cooked-turkey-breast-slices`` (fiambre ≠ raw breast), ``canned-peppers``
# (≠ fresh pepper), ``serrano-ham`` / ``jamon-curado`` (cured ≠ cooked ham), ``lemon-yogurts``
# (≠ plain yogurt), ``mixed-cereal-flakes`` (≠ pure oats).
OFF_CATEGORY_TO_CANONICAL: dict[str, tuple[str, str]] = {
    # category_code            -> (canonical_name, confidence)
    "basmati-rices": ("arroz_basmati", "0.92"),
    "white-rices": ("arroz_redondo", "0.85"),
    "wholemeal-breads": ("pan_integral", "0.90"),
    "canned-chickpeas": ("garbanzos_cocido", "0.90"),
    "chickpeas": ("garbanzos_seco", "0.85"),
    "peanuts": ("cacahuete", "0.90"),
    "walnuts": ("nuez", "0.90"),
    "almonds": ("almendra", "0.90"),
    "cashew-nuts": ("anacardo", "0.90"),
    "cider-vinegars": ("vinagre", "0.85"),
    "wine-vinegars": ("vinagre", "0.85"),
    "vinegars": ("vinagre", "0.80"),
    "cow-milks": ("leche_entera", "0.72"),  # fat level unstated; whole is the default milk
    "whole-milks": ("leche_entera", "0.90"),
    "semi-skimmed-milks": ("leche_desnatada", "0.75"),
    "skimmed-milks": ("leche_desnatada", "0.90"),
    "extra-virgin-olive-oils": ("aceite_oliva", "0.92"),
    "olive-oils": ("aceite_oliva", "0.88"),
    "sunflower-oils": ("aceite_girasol", "0.90"),
    "tofu": ("tofu", "0.92"),
    "coconut-milks": ("leche_coco", "0.88"),
    "honeys": ("miel", "0.88"),
    "wheat-flours": ("harina_trigo", "0.88"),
}


# --------------------------------------------------------------------------- #
# Name synonym rules (curated Spanish keyword table)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _NameRule:
    """One name-match alternative for a canonical ingredient.

    Matches a product when every token in ``required`` is present (see :func:`_has_required`)
    and none of ``forbidden`` is. ``confidence`` is the score awarded on a match.
    """

    canonical: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]
    confidence: str


# Guard tokens that turn a raw-ingredient name into a processed/snack product: if any is
# present, a raw fruit/veg/nut/dairy match is rejected (nougat, chocolate, biscuit, sauce…).
_SNACK_GUARD: tuple[str, ...] = (
    "turron", "bombon", "chocolate", "choco", "choc", "galleta", "galletas", "gall",
    "barrita", "barritas", "crema", "salsa", "pesto", "bolonesa", "napolitana",
    "carbonara", "ketchup", "snack", "palomitas", "tortitas", "tortita", "licor",
    "whisky", "ron", "vodka", "cerveza", "refresco", "batido", "helado", "pizza",
    "mazapan", "chips", "xips", "mermelada", "confitura", "pastel", "tarta", "bebida",
    "frito", "fritas", "frita", "aromatizada", "aromatizado", "aperitivo",
)


def _rules() -> list[_NameRule]:
    r = _NameRule
    g = _SNACK_GUARD
    return [
        # --- verduras ---
        r("tomate", ("tomate", "triturado"), (), "0.88"),
        r("tomate", ("tomate", "natural"), ("frito", "salsa"), "0.78"),
        r("cebolla", ("cebolla",), (*g, "frito", "aros"), "0.80"),
        r("ajo", ("ajo",), (*g, "alioli", "salsa"), "0.80"),
        r("pimiento_rojo", ("pimiento", "rojo"), ("piquillo", "salsa"), "0.80"),
        r("calabacin", ("calabacin",), g, "0.82"),
        r("zanahoria", ("zanahoria",), g, "0.82"),
        r("espinaca", ("espinaca",), g, "0.82"),
        r("patata", ("patata",), (*g, "puré", "pure", "chips", "panadera"), "0.78"),
        # --- frutas ---
        r("manzana", ("manzana",), (*g, "zumo", "compota"), "0.80"),
        r("platano", ("platano",), g, "0.82"),
        r("naranja", ("naranja",), (*g, "zumo", "refresco"), "0.78"),
        r("fresa", ("fresa",), g, "0.82"),
        r("fresa", ("freson",), g, "0.80"),
        r("limon", ("limon",), (*g, "zumo"), "0.78"),
        r("aguacate", ("aguacate",), g, "0.85"),
        r("arandano", ("arandano",), g, "0.82"),
        r("arandano", ("nabius",), g, "0.75"),  # ca: nabius = arándanos
        # --- carne ---
        r("pollo_pechuga", ("pechuga", "pollo"), ("empanada", "fiambre"), "0.85"),
        r("pollo_muslo", ("muslo", "pollo"), (), "0.85"),
        r("pollo_muslo", ("contramuslo", "pollo"), (), "0.82"),
        r("ternera_picada", ("ternera", "picada"), (), "0.85"),
        r("ternera_picada", ("picada", "vacuno"), (), "0.80"),
        r("cerdo_lomo", ("lomo", "cerdo"), ("embuchado", "caña", "cana"), "0.82"),
        r("pavo_pechuga", ("pechuga", "pavo"), ("fiambre", "lonchas", "cocida"), "0.85"),
        r("chorizo", ("chorizo",), ("sabor",), "0.82"),
        r("jamon_cocido", ("jamon", "cocido"), (), "0.85"),
        r("jamon_cocido", ("jamon", "york"), (), "0.85"),
        # --- pescado / marisco ---
        r("salmon", ("salmon",), (*g, "sabor"), "0.82"),
        r("merluza", ("merluza",), (), "0.85"),
        r("atun_lata", ("atun",), ("sabor", "pate"), "0.80"),
        r("gambas", ("gambas",), (), "0.82"),
        r("gambas", ("langostino",), (), "0.72"),
        r("bacalao", ("bacalao",), (), "0.82"),
        r("mejillones", ("mejillones",), (), "0.85"),
        r("mejillones", ("mejillon",), (), "0.82"),
        # --- lacteos ---
        r("leche_entera", ("leche", "entera"), ("coco", "avena", "soja", "almendra"), "0.85"),
        r("leche_desnatada", ("leche", "desnatada"), (), "0.85"),
        r("leche_desnatada", ("leche", "semidesnatada"), (), "0.78"),
        r("yogur_natural", ("yogur", "natural"), ("sabor", "griego", "azucar"), "0.82"),
        r("queso_curado", ("queso", "curado"), (), "0.82"),
        r("queso_fresco", ("queso", "fresco"), (), "0.82"),
        r("mantequilla", ("mantequilla",), ("cacahuete", "ajo"), "0.82"),
        r("nata_cocinar", ("nata", "cocinar"), (), "0.85"),
        r("nata_cocinar", ("nata", "cocina"), (), "0.85"),
        # --- huevos ---
        r("huevos_l", ("huevos", "grandes"), (), "0.82"),
        r("huevos_l", ("huevos", "xl"), (), "0.80"),
        r("huevos_m", ("huevos", "medianos"), (), "0.82"),
        # --- cereales / pasta / arroz ---
        r("arroz_basmati", ("arroz", "basmati"), (), "0.88"),
        r("arroz_redondo", ("arroz", "redondo"), (), "0.85"),
        r("arroz_redondo", ("arroz", "bomba"), (), "0.80"),
        r("pasta_macarrones", ("macarrones",), ("salsa",), "0.82"),
        r("pasta_espagueti", ("espagueti",), ("salsa",), "0.82"),
        r("pasta_espagueti", ("espaguetis",), ("salsa",), "0.82"),
        r("avena_copos", ("copos", "avena"), (), "0.85"),
        r("avena_copos", ("avena", "integral"), (), "0.78"),
        r("quinoa", ("quinoa",), (*g, "chips"), "0.82"),
        r("cuscus", ("cuscus",), (), "0.85"),
        r("cuscus", ("couscous",), (), "0.82"),
        # --- legumbres ---
        r("garbanzos_cocido", ("garbanzos", "cocidos"), (), "0.85"),
        r("garbanzos_cocido", ("garbanzos", "bote"), (), "0.82"),
        r("garbanzos_seco", ("garbanzos", "secos"), (), "0.82"),
        r("lentejas", ("lentejas",), ("sopa",), "0.80"),
        r("alubias_blancas", ("alubias", "blancas"), (), "0.85"),
        r("alubias_blancas", ("alubia", "blanca"), (), "0.82"),
        r("guisantes", ("guisantes",), g, "0.82"),
        r("soja_texturizada", ("soja", "texturizada"), (), "0.85"),
        # --- panaderia ---
        r("pan_barra", ("barra", "pan"), (), "0.80"),
        r("pan_molde", ("pan", "molde"), (), "0.85"),
        r("pan_integral", ("pan", "integral"), (), "0.85"),
        r("tortillas_trigo", ("tortillas", "trigo"), (), "0.85"),
        r("tortillas_trigo", ("tortillas", "wrap"), (), "0.78"),
        r("biscotes", ("biscotes",), (), "0.82"),
        # --- aceites / condimentos ---
        # Guard against fish canned *in* oil (``ATUN … ACEITE OLIVA`` is tuna, not oil).
        r("aceite_oliva", ("aceite", "oliva"),
          ("atun", "bonito", "sardina", "sardinas", "mejillon", "mejillones",
           "ventresca", "caballa", "anchoa", "anchoas", "berberechos"), "0.88"),
        r("aceite_girasol", ("aceite", "girasol"),
          ("atun", "bonito", "sardina", "sardinas", "mejillon", "mejillones"), "0.88"),
        # Table salt only — never a salted snack (``PIPAS … SAL``, ``PATATAS … SAL``).
        r("sal", ("sal",),
          ("salsa", "salmon", "salchicha", "salchichon", "pipa", "pipas", "gir",
           "girasol", "patata", "patatas", "frito", "frita", "fritas", "tostado",
           "tostada", "tostadas", "cacahuete", "snack", "aperitivo", "aladas",
           "calaba"), "0.75"),
        r("pimienta_negra", ("pimienta", "negra"), (), "0.85"),
        r("pimenton", ("pimenton",), (), "0.85"),
        r("comino", ("comino",), (), "0.85"),
        r("vinagre", ("vinagre",), (), "0.85"),
        # --- frutos secos / semillas ---
        r("almendra", ("almendra",), (*g, "leche", "bebida", "harina"), "0.80"),
        r("almendra", ("almendras",), (*g, "leche", "bebida", "harina"), "0.80"),
        r("nuez", ("nuez",), (*g, "moscada", "leche"), "0.80"),
        r("nuez", ("nueces",), (*g, "moscada", "leche"), "0.80"),
        r("anacardo", ("anacardo",), g, "0.82"),
        r("anacardo", ("anacardos",), g, "0.82"),
        r("cacahuete", ("cacahuete",), (*g, "mantequilla", "salsa"), "0.80"),
        r("cacahuete", ("cacahuetes",), (*g, "mantequilla", "salsa"), "0.80"),
        r("sesamo", ("sesamo",), (*g, "aceite", "salsa"), "0.80"),
        # --- conservas / despensa ---
        r("tomate_triturado", ("tomate", "triturado"), (), "0.88"),
        r("maiz_dulce", ("maiz", "dulce"), ("harina",), "0.82"),
        r("aceitunas", ("aceitunas",), (), "0.82"),
        r("aceitunas", ("aceituna",), (), "0.80"),
        r("harina_trigo", ("harina", "trigo"), (), "0.85"),
        r("azucar", ("azucar",), (*g, "sin", "glas", "avainillado", "cafe"), "0.75"),
        r("miel", ("miel",), (*g, "mostaza", "moutarde", "maille", "vinagreta"), "0.80"),
        r("tofu", ("tofu",), (), "0.85"),
        r("leche_coco", ("leche", "coco"), (), "0.85"),
        r("leche_coco", ("crema", "coco"), (), "0.78"),
    ]


# Precomputed once; module-level rule table (grouped for the ``best`` scan).
_RULES: list[_NameRule] = _rules()

# --------------------------------------------------------------------------- #
# Unit compatibility
# --------------------------------------------------------------------------- #
_VOLUME_UNITS = frozenset({"ml", "l", "cl", "litro", "litros", "lt"})
_MASS_UNITS = frozenset({"g", "gr", "grs", "kg", "kgs", "mg", "gramo", "gramos"})


def _ingredient_is_liquid(ingredient: Ingredient) -> bool:
    return (ingredient.default_unit or "").lower() in _VOLUME_UNITS


def _product_unit_family(db: Session, product: Product) -> str | None:
    """Best-effort mass/volume family for a product, from its package unit or latest price.

    Returns ``"mass"``, ``"volume"`` or ``None`` (unknown — e.g. a ``unit``/``None`` basis,
    which is compatible with anything). Used only to *reject* a clear liquid/solid conflict.
    """
    unit = (product.package_unit or "").lower()
    if not unit:
        price_unit = db.execute(
            select(ProductPrice.package_unit)
            .where(ProductPrice.product_id == product.id)
            .order_by(ProductPrice.observed_at.desc(), ProductPrice.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        unit = (price_unit or "").lower()
    if unit in _VOLUME_UNITS:
        return "volume"
    if unit in _MASS_UNITS:
        return "mass"
    return None


def _unit_compatible(db: Session, product: Product, ingredient: Ingredient) -> bool:
    """False only on a clear conflict (liquid ingredient vs mass product, or vice versa)."""
    family = _product_unit_family(db, product)
    if family is None:
        return True
    return _ingredient_is_liquid(ingredient) == (family == "volume")


# --------------------------------------------------------------------------- #
# Ingredient index
# --------------------------------------------------------------------------- #
def _load_ingredient_index(db: Session) -> dict[str, Ingredient]:
    """``{canonical_name: Ingredient}`` for every canonical ingredient in the DB."""
    rows = db.execute(select(Ingredient)).scalars().all()
    return {ing.canonical_name: ing for ing in rows}


# --------------------------------------------------------------------------- #
# Core matcher
# --------------------------------------------------------------------------- #
def _best_candidate(
    product: Product, index: dict[str, Ingredient]
) -> tuple[Ingredient, Decimal] | None:
    """Best (ingredient, confidence) for ``product`` from the category map + name rules.

    Category hits and name-rule hits compete on confidence; the highest wins (ties broken by
    rule specificity — more required tokens). Ignores the unit check and the threshold; the
    caller applies those.
    """
    best: tuple[Ingredient, Decimal, int] | None = None

    def _offer(canonical: str, confidence: str, specificity: int) -> None:
        nonlocal best
        ingredient = index.get(canonical)
        if ingredient is None:
            return
        conf = Decimal(confidence)
        if best is None or (conf, specificity) > (best[1], best[2]):
            best = (ingredient, conf, specificity)

    # 1) OFF category (specificity 3 so it outranks a same-confidence single-token name rule).
    category = (product.category_code or "").strip().lower()
    if category:
        entry = OFF_CATEGORY_TO_CANONICAL.get(category)
        if entry is not None:
            _offer(entry[0], entry[1], 3)

    # 2) Name token / keyword rules.
    tokens = _normalize_tokens(product.name or "")
    if tokens:
        for rule in _RULES:
            if any(_has_required(f, tokens) for f in rule.forbidden):
                continue
            if all(_has_required(req, tokens) for req in rule.required):
                _offer(rule.canonical, rule.confidence, len(rule.required))

    if best is None:
        return None
    return best[0], best[1]


def match_product(
    db: Session,
    product: Product,
    *,
    ingredient_index: dict[str, Ingredient] | None = None,
    min_confidence: Decimal = DEFAULT_MIN_CONFIDENCE,
) -> tuple[Ingredient, Decimal] | None:
    """Match one real ``product`` to a canonical :class:`Ingredient` + confidence, or ``None``.

    Strategy (conservative, in order): curated OFF-category map → curated Spanish name
    token/keyword table → unit-compatibility sanity check. Returns ``None`` when nothing
    matched, the best candidate fails the unit check, or its confidence is below
    ``min_confidence``. Never guesses; ``ingredient_index`` may be supplied to avoid
    re-querying when matching many products.
    """
    index = ingredient_index if ingredient_index is not None else _load_ingredient_index(db)
    candidate = _best_candidate(product, index)
    if candidate is None:
        return None
    ingredient, confidence = candidate
    if confidence < min_confidence:
        return None
    if not _unit_compatible(db, product, ingredient):
        return None
    return ingredient, confidence


# --------------------------------------------------------------------------- #
# Bulk mapping
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class MappingSummary:
    """Outcome of a :func:`map_real_products` run."""

    scanned: int = 0
    mapped: int = 0
    skipped_already_mapped: int = 0
    unmatched: int = 0
    #: {ChainName: mapped_count}
    per_chain: dict[str, int] = field(default_factory=dict)
    #: A few representative mappings for the report (quality spot-check).
    samples: list[dict[str, str]] = field(default_factory=list)
    #: Per-chain ingredient coverage after mapping (see :func:`chain_ingredient_coverage`).
    #: Pricing is by chain (retailer), not by individual store, so coverage is reported at
    #: chain level: how many canonical ingredients are mapped AND priced somewhere in the chain.
    chain_coverage: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "mapped": self.mapped,
            "skipped_already_mapped": self.skipped_already_mapped,
            "unmatched": self.unmatched,
            "per_chain": dict(self.per_chain),
            "samples": list(self.samples),
            "chain_coverage": list(self.chain_coverage),
        }


_SAMPLE_CAP = 25


def _unmapped_real_products(db: Session, store_id: int | None) -> list[Product]:
    """Real, live products with no active-or-inactive mapping yet.

    Scoped to products priced at ``store_id`` when given (so an admin can map just the
    store they care about); otherwise every real product lacking a mapping.
    """
    mapped_ids = select(IngredientProductMapping.product_id)
    stmt = (
        select(Product)
        .where(
            Product.is_synthetic.is_(False),
            Product.deleted_at.is_(None),
            Product.id.not_in(mapped_ids),
        )
        .order_by(Product.id)
    )
    if store_id is not None:
        priced_here = select(ProductPrice.product_id).where(
            ProductPrice.store_id == store_id
        )
        stmt = stmt.where(Product.id.in_(priced_here))
    return list(db.execute(stmt).scalars().all())


def map_real_products(
    db: Session,
    *,
    store_id: int | None = None,
    min_confidence: Decimal = DEFAULT_MIN_CONFIDENCE,
) -> MappingSummary:
    """Match every unmapped real product and INSERT an :class:`IngredientProductMapping`.

    Idempotent: products that already carry a mapping are skipped, so re-running maps only
    newly-synced products and never duplicates a row. Only matches at or above
    ``min_confidence`` are written, each with its ``confidence_score`` and the product's
    ``retailer_id``. Flushes but does not commit.
    """
    summary = MappingSummary()
    index = _load_ingredient_index(db)
    retailer_names: dict[int, str] = {}

    for product in _unmapped_real_products(db, store_id):
        summary.scanned += 1
        result = match_product(
            db, product, ingredient_index=index, min_confidence=min_confidence
        )
        if result is None:
            summary.unmatched += 1
            continue
        ingredient, confidence = result
        db.add(
            IngredientProductMapping(
                ingredient_id=ingredient.id,
                product_id=product.id,
                retailer_id=product.retailer_id,
                confidence_score=confidence,
                is_active=True,
            )
        )
        summary.mapped += 1
        chain = _retailer_name(db, product.retailer_id, retailer_names)
        summary.per_chain[chain] = summary.per_chain.get(chain, 0) + 1
        if len(summary.samples) < _SAMPLE_CAP:
            summary.samples.append(
                {
                    "chain": chain,
                    "product": product.name,
                    "ingredient": ingredient.canonical_name,
                    "confidence": f"{confidence:.2f}",
                }
            )
    db.flush()
    return summary


def _retailer_name(db: Session, retailer_id: int | None, cache: dict[int, str]) -> str:
    if retailer_id is None:
        return "?"
    if retailer_id not in cache:
        retailer = db.get(Retailer, retailer_id)
        cache[retailer_id] = retailer.name if retailer is not None else "?"
    return cache[retailer_id]


# --------------------------------------------------------------------------- #
# Coverage reporting (CHAIN-scoped — pricing is by retailer, not by single store)
# --------------------------------------------------------------------------- #
def chain_ingredient_coverage(db: Session, retailer_id: int) -> dict[str, object]:
    """How many distinct canonical ingredients are mapped AND priced across a whole chain.

    Pricing is resolved at the chain (retailer) level: a product priced in *any* of the
    chain's stores counts. This aggregates all the chain's stores' observations and counts the
    canonical ingredients that have at least one actively-mapped, non-deleted product with a
    price somewhere in the chain — i.e. exactly the ingredients the planner can cost with a
    real price for that chain (out of the recipe's ~75-ingredient vocabulary).
    """
    retailer = db.get(Retailer, retailer_id)
    total = db.execute(select(func.count(Ingredient.id))).scalar_one()
    rows = db.execute(
        select(Ingredient.canonical_name)
        .join(
            IngredientProductMapping,
            IngredientProductMapping.ingredient_id == Ingredient.id,
        )
        .join(Product, Product.id == IngredientProductMapping.product_id)
        .join(ProductPrice, ProductPrice.product_id == Product.id)
        .where(
            IngredientProductMapping.is_active.is_(True),
            Product.deleted_at.is_(None),
            ProductPrice.retailer_id == retailer_id,
        )
        .distinct()
    ).scalars().all()
    priced = sorted(rows)
    return {
        "retailer_id": retailer_id,
        "chain": retailer.name if retailer is not None else "?",
        "priced_ingredients": len(priced),
        "total_ingredients": total,
        "ingredients": priced,
    }


def retailers_with_prices(db: Session) -> list[int]:
    """Distinct retailer ids that carry at least one price observation."""
    return list(
        db.execute(select(ProductPrice.retailer_id).distinct()).scalars().all()
    )


def all_chain_coverage(db: Session) -> list[dict[str, object]]:
    """Chain-level ingredient coverage for every chain that has priced products.

    One :func:`chain_ingredient_coverage` entry per retailer with prices, ordered by how many
    canonical ingredients are priced (most first). This is the honest, chain-scoped coverage
    the planner can realise once real products are mapped.
    """
    reports = [
        chain_ingredient_coverage(db, retailer_id)
        for retailer_id in retailers_with_prices(db)
    ]
    reports.sort(key=lambda r: r["priced_ingredients"], reverse=True)  # type: ignore[arg-type,return-value]
    return reports
