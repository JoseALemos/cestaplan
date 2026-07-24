"""Ingredient dictionary + deterministic mapping-candidate rules (spec §4/§6).

Aliases NEVER auto-approve on their own. A mapping is approved only when deterministic rules
pass: the product must carry every REQUIRED term, none of the EXCLUDING/forbidden-form terms,
a compatible category and a compatible unit. This is what stops "aceite" -> olive oil,
"tomate" -> crushed tomato, "avena" -> oat drink, "ajo" -> garlic powder, etc.

A semantic model MAY propose candidates elsewhere, but it is never the sole evidence to approve
(``semantic_score`` is advisory only; ``confidence`` here is rule-based).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class IngredientSpec:
    key: str
    category_code: str
    aliases: tuple[str, ...]  # positive phrases (normalized) that identify the ingredient
    required_terms: tuple[str, ...]  # ALL must appear (e.g. "oliva" for olive oil)
    excluding_terms: tuple[str, ...]  # ANY present -> a DIFFERENT product (reject/incompatible)
    forbidden_forms: tuple[str, ...]  # wrong format/preparation/state (reject)
    allowed_units: tuple[str, ...]  # compatible net-content units
    allergens: tuple[str, ...] = ()


# Priority-ingredient dictionary. Excluding/forbidden terms encode the "dangerous word" cases.
_SPECS: dict[str, IngredientSpec] = {
    "aceite_oliva": IngredientSpec(
        "aceite_oliva",
        "aceites_condimentos",
        aliases=(
            "aceite de oliva",
            "aceite oliva",
            "aceite de oliva virgen",
            "aceite de oliva virgen extra",
            "aove",
        ),
        required_terms=("aceite", "oliva"),
        excluding_terms=("girasol", "semillas", "maiz", "palma", "coco", "soja", "orujo"),
        forbidden_forms=("spray", "aromatizado"),
        allowed_units=("l", "ml"),
    ),
    "sal": IngredientSpec(
        "sal",
        "aceites_condimentos",
        aliases=("sal", "sal fina", "sal marina", "sal yodada"),
        required_terms=("sal",),
        excluding_terms=("salsa", "salchicha", "salmon", "salame", "ensalada", "salado"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "cebolla": IngredientSpec(
        "cebolla",
        "verduras",
        aliases=("cebolla", "cebolla blanca", "cebolla dulce", "cebolla amarilla"),
        required_terms=("cebolla",),
        excluding_terms=("cebollino",),
        forbidden_forms=("frita", "en polvo", "deshidratada", "crujiente", "encurtida"),
        allowed_units=("g", "kg", "unit"),
    ),
    "ajo": IngredientSpec(
        "ajo",
        "verduras",
        aliases=("ajo", "cabeza de ajo", "dientes de ajo", "ajos"),
        required_terms=("ajo",),
        excluding_terms=("ajedrea",),
        forbidden_forms=("en polvo", "molido", "granulado", "deshidratado", "frito"),
        allowed_units=("g", "kg", "unit"),
    ),
    "tomate": IngredientSpec(
        "tomate",
        "verduras",
        aliases=("tomate", "tomate pera", "tomate rama", "tomate ensalada"),
        required_terms=("tomate",),
        excluding_terms=(),
        forbidden_forms=(
            "triturado",
            "frito",
            "concentrado",
            "ketchup",
            "salsa",
            "seco",
            "en conserva",
            "pelado",
        ),
        allowed_units=("g", "kg", "unit"),
    ),
    "avena_copos": IngredientSpec(
        "avena_copos",
        "cereales_pasta_arroz",
        aliases=("copos de avena", "avena en copos", "copos avena"),
        required_terms=("avena", "copos"),
        excluding_terms=("bebida", "drink", "galleta", "barrita", "harina", "cereales", "cereal"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "espinaca": IngredientSpec(
        "espinaca",
        "verduras",
        aliases=("espinaca", "espinacas", "espinaca fresca", "espinaca congelada"),
        required_terms=("espinaca",),
        excluding_terms=(),
        forbidden_forms=(
            "crema",
            "lasaña",
            "lasana",
            "plato",
            "preparado",
            "salteado",
            "empanadilla",
            "pizza",
        ),
        allowed_units=("g", "kg"),
    ),
    "patata": IngredientSpec(
        "patata",
        "verduras",
        aliases=("patata", "patatas", "patata para cocer", "patata para guisar"),
        required_terms=("patata",),
        excluding_terms=("patatilla",),
        forbidden_forms=("frita", "chips", "prefrita", "pure", "snack", "gajos", "congelada"),
        allowed_units=("g", "kg", "unit"),
    ),
    # Recipe-targeted ingredients (spec §6). Milk variants are multi-term (deterministic by name);
    # fruit is single-word (needs a confirmed category). Vegetal drinks / flavoured / prepared
    # products are excluded so "leche" != oat drink, "platano" != banana milkshake, etc.
    "leche_entera": IngredientSpec(
        "leche_entera",
        "lacteos",
        aliases=("leche entera", "leche entera de vaca"),
        required_terms=("leche", "entera"),
        excluding_terms=(
            "avena",
            "soja",
            "almendra",
            "coco",
            "arroz",
            "condensada",
            "evaporada",
            "polvo",
            "bebida",
            "desnatada",
            "semidesnatada",
        ),
        forbidden_forms=("en polvo",),
        allowed_units=("l", "ml"),
        allergens=("milk",),
    ),
    "leche_desnatada": IngredientSpec(
        "leche_desnatada",
        "lacteos",
        aliases=("leche desnatada", "leche desnatada de vaca"),
        required_terms=("leche", "desnatada"),
        excluding_terms=(
            "avena",
            "soja",
            "almendra",
            "coco",
            "arroz",
            "condensada",
            "evaporada",
            "polvo",
            "bebida",
            "entera",
            "semidesnatada",
        ),
        forbidden_forms=("en polvo",),
        allowed_units=("l", "ml"),
        allergens=("milk",),
    ),
    "platano": IngredientSpec(
        "platano",
        "frutas",
        aliases=("platano", "platano de canarias", "banana"),
        required_terms=("platano",),
        excluding_terms=(
            "sabor",
            "batido",
            "yogur",
            "chips",
            "frito",
            "macho",
            "deshidratado",
            "pure",
            "infantil",
        ),
        forbidden_forms=("preparado", "crujiente"),
        allowed_units=("g", "kg", "unit"),
    ),
    "arandano": IngredientSpec(
        "arandano",
        "frutas",
        aliases=("arandano", "arandanos"),
        required_terms=("arandano",),
        excluding_terms=("mermelada", "zumo", "yogur", "sabor", "recubierto", "azucarado"),
        forbidden_forms=("preparado",),
        allowed_units=("g", "kg"),
    ),
    "yogur_natural": IngredientSpec(
        "yogur_natural",
        "lacteos",
        aliases=("yogur natural", "yogures naturales"),
        required_terms=("yogur", "natural"),
        excluding_terms=(
            "sabor",
            "fresa",
            "limon",
            "coco",
            "griego azucarado",
            "natillas",
            "kefir",
            "bebible",
            "liquido",
            "azucarado",
            "fruta",
        ),
        forbidden_forms=(),
        allowed_units=("g", "kg", "ml", "l"),
        allergens=("milk",),
    ),
}

# Deterministic map of a provider's own category text -> our internal category code. Used so a
# single-word ingredient can be auto-approved only when the provider category is compatible.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "frutas": ("fruta", "platano", "banana", "arandano", "manzana", "naranja", "pera"),
    "verduras": (
        "verdura",
        "hortaliza",
        "tomate",
        "cebolla",
        "ajo",
        "espinaca",
        "patata",
        "lechuga",
    ),
    "lacteos": ("lacteo", "leche", "yogur", "queso", "nata", "mantequilla"),
    "aceites_condimentos": ("aceite", "sal ", "vinagre", "especia", "condimento", "aliño"),
    "cereales_pasta_arroz": ("cereal", "avena", "arroz", "pasta", "harina", "copos"),
}


def normalize_provider_category(text: str | None) -> str | None:
    """Map a provider category string (e.g. 'Frutas y Verduras/Fruta') to our code, or None."""
    if not text:
        return None
    n = normalize(text)
    for code, words in _CATEGORY_KEYWORDS.items():
        if any(w.strip() in n for w in words):
            return code
    return None


def specs() -> dict[str, IngredientSpec]:
    return _SPECS


def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace — deterministic and reproducible."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize(text)))


@dataclass(slots=True)
class MappingCandidate:
    ingredient_key: str
    lexical_score: Decimal
    category_score: Decimal
    semantic_score: Decimal | None  # advisory only; never the sole approval evidence
    confidence: Decimal
    mapping_status: str  # candidate | auto_approved | ambiguous | rejected | incompatible
    mapping_method: str
    unit_compatibility: str
    preparation_compatibility: str
    dietary_compatibility: str
    allergen_compatibility: str
    required_review: bool
    matched_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ingredient_key": self.ingredient_key,
            "lexical_score": str(self.lexical_score),
            "category_score": str(self.category_score),
            "semantic_score": None if self.semantic_score is None else str(self.semantic_score),
            "confidence": str(self.confidence),
            "mapping_status": self.mapping_status,
            "mapping_method": self.mapping_method,
            "unit_compatibility": self.unit_compatibility,
            "preparation_compatibility": self.preparation_compatibility,
            "dietary_compatibility": self.dietary_compatibility,
            "allergen_compatibility": self.allergen_compatibility,
            "required_review": self.required_review,
            "matched_rules": list(self.matched_rules),
            "warnings": list(self.warnings),
        }


def _unit_compatibility(spec: IngredientSpec, unit: str | None) -> str:
    if unit is None:
        return "unknown"
    u = unit.lower()
    if u in spec.allowed_units:
        return "compatible"
    convertible = {"l": {"ml"}, "ml": {"l"}, "kg": {"g"}, "g": {"kg"}}
    if any(u in convertible.get(a, set()) for a in spec.allowed_units):
        return "convertible"
    return "incompatible"


def classify_mapping(
    ingredient_key: str,
    *,
    product_name: str,
    brand: str | None = None,
    category_code: str | None = None,
    net_content_unit: str | None = None,
    semantic_score: Decimal | None = None,
) -> MappingCandidate:
    """Deterministic candidate classification. Aliases alone never approve — required terms,
    excluding terms, forbidden forms, category and unit all gate the outcome."""
    spec = _SPECS[ingredient_key]
    name_norm = f"{normalize(product_name)} {normalize(brand or '')}".strip()
    toks = _tokens(name_norm)
    rules: list[str] = []
    warnings: list[str] = []

    # 1. hard exclusions: a different product entirely.
    hit_exclude = [t for t in spec.excluding_terms if t in toks]
    hit_forbidden = [f for f in spec.forbidden_forms if normalize(f) in name_norm]
    prep_compat = "incompatible" if hit_forbidden else "compatible"
    if hit_exclude or hit_forbidden:
        return MappingCandidate(
            ingredient_key,
            Decimal("0"),
            Decimal("0"),
            semantic_score,
            Decimal("0"),
            mapping_status="incompatible",
            mapping_method="category_constrained",
            unit_compatibility=_unit_compatibility(spec, net_content_unit),
            preparation_compatibility=prep_compat,
            dietary_compatibility="unknown",
            allergen_compatibility="compatible",
            required_review=False,
            matched_rules=rules,
            warnings=[f"excluding term(s): {hit_exclude or hit_forbidden}"],
        )

    # 2. lexical: exact alias phrase vs required-term coverage vs bare generic word.
    exact_alias = any(
        normalize(a) in name_norm for a in spec.aliases if len(a) > 3 or a == spec.key
    )
    # Plural-tolerant term match: "ajo" matches "ajos", "tomate" matches "tomates".
    required_hits = sum(1 for t in spec.required_terms if toks & {t, f"{t}s", f"{t}es"})
    required_ratio = Decimal(required_hits) / Decimal(len(spec.required_terms))
    if exact_alias:
        rules.append("exact_alias")
    lexical = Decimal("1.0") if exact_alias else (required_ratio * Decimal("0.8"))

    # 3. category + unit compatibility.
    unit_compat = _unit_compatibility(spec, net_content_unit)
    if category_code is None:
        category_score = Decimal("0.5")  # unknown category -> neutral, needs review
    elif category_code == spec.category_code:
        category_score = Decimal("1.0")
        rules.append("category_match")
    else:
        category_score = Decimal("0")
        return MappingCandidate(
            ingredient_key,
            lexical,
            category_score,
            semantic_score,
            Decimal("0"),
            mapping_status="incompatible",
            mapping_method="category_constrained",
            unit_compatibility=unit_compat,
            preparation_compatibility=prep_compat,
            dietary_compatibility="unknown",
            allergen_compatibility="compatible",
            required_review=False,
            matched_rules=rules,
            warnings=[f"category {category_code} != {spec.category_code}"],
        )

    # 4. confidence — deterministic combination. A bare generic word (required incomplete,
    #    no exact alias) can never reach the auto-approval band.
    all_required = required_hits == len(spec.required_terms)
    confidence = (lexical * Decimal("0.6") + category_score * Decimal("0.25")) + (
        Decimal("0.15") if unit_compat in ("compatible", "convertible") else Decimal("0")
    )
    confidence = min(confidence, Decimal("1.0")).quantize(Decimal("0.0001"))

    method = "exact_alias" if exact_alias else "normalized_name"
    # A single-word required term ("sal", "ajo", "tomate"...) is generic: the word alone is
    # NEVER enough to auto-approve — it needs a CONFIRMED category. A multi-term name
    # ("aceite" + "oliva", "avena" + "copos") is inherently specific and deterministic.
    specific_name = len(spec.required_terms) >= 2
    category_confirmed = category_score >= Decimal("1.0")
    deterministic = exact_alias and all_required and unit_compat in ("compatible", "convertible")
    if deterministic and (specific_name or category_confirmed):
        status, review = "auto_approved", False
        confidence = max(confidence, Decimal("0.9600"))
        rules.append("deterministic_auto_approve")
    elif confidence >= Decimal("0.75") and all_required:
        status, review = "candidate", True
        warnings.append("generic term needs a confirmed category or manual review")
    else:
        status, review = "ambiguous", True
        if not all_required:
            warnings.append("generic match: required terms incomplete — never auto-approved")

    return MappingCandidate(
        ingredient_key,
        lexical.quantize(Decimal("0.0001")),
        category_score.quantize(Decimal("0.0001")),
        semantic_score,
        confidence,
        mapping_status=status,
        mapping_method=method,
        unit_compatibility=unit_compat,
        preparation_compatibility=prep_compat,
        dietary_compatibility="unknown",
        allergen_compatibility="compatible",
        required_review=review,
        matched_rules=rules,
        warnings=warnings,
    )


__all__ = [
    "IngredientSpec",
    "MappingCandidate",
    "classify_mapping",
    "normalize",
    "normalize_provider_category",
    "specs",
]
