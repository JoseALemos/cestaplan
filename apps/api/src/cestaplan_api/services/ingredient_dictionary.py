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
    # ------------------------------------------------------------------------------------- #
    # Fase 2: highest-frequency ingredients of the 100 imported recipes that lacked a spec.
    # Every key below is EXACTLY an existing ``ingredient.canonical_name`` (validated by
    # ``tests/services/test_spec_ingredient_rows.py`` against a read-only prod snapshot). Keys
    # keep the canonical form of the row (accents/spaces included) so recipe<->spec identity
    # holds. Single-word specs are generic on purpose and can NEVER auto-approve without a
    # confirmed category (see ``classify_mapping``); multi-word specs (2+ required terms) are
    # specific and must NOT be swallowed by their generic sibling — the excluding terms encode
    # that separation (``pimiento`` vs ``pimiento_rojo``, ``leche`` vs ``leche_entera``,
    # ``aceite`` vs ``aceite_oliva``, ``caldo`` vs ``caldo de pescado``, ``pan`` vs
    # ``pan rallado``, ``pimienta`` vs ``pimiento``...).
    # --- Basics / oils / condiments --- #
    "aceite": IngredientSpec(
        "aceite",
        "aceites_condimentos",
        # Generic cooking oil. NOT a substitute for the specific ``aceite_oliva`` /
        # ``aceite_girasol`` rows: those are distinct ingredients with their own (2-term) match.
        aliases=("aceite", "aceite vegetal", "aceite de cocina"),
        required_terms=("aceite",),
        excluding_terms=("motor", "corporal", "masaje", "parafina", "esencial"),
        forbidden_forms=("spray", "aromatizado"),
        allowed_units=("l", "ml"),
    ),
    "vinagre": IngredientSpec(
        "vinagre",
        "aceites_condimentos",
        aliases=("vinagre", "vinagre de vino", "vinagre de manzana", "vinagre de modena"),
        required_terms=("vinagre",),
        excluding_terms=("aceite",),
        forbidden_forms=(),
        allowed_units=("l", "ml"),
    ),
    "azucar": IngredientSpec(
        "azucar",
        "panaderia_reposteria",
        aliases=("azucar", "azucar blanco", "azucar moreno", "azucar glas"),
        required_terms=("azucar",),
        excluding_terms=("sacarina", "edulcorante", "stevia", "vainillado"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "sal gorda": IngredientSpec(
        "sal gorda",
        "aceites_condimentos",
        aliases=("sal gorda", "sal gruesa"),
        required_terms=("sal", "gorda"),
        excluding_terms=("salsa", "ensalada"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    # --- Spices / herbs --- #
    "pimenton": IngredientSpec(
        "pimenton",
        "aceites_condimentos",
        aliases=("pimenton", "pimenton dulce", "pimenton picante", "pimenton de la vera"),
        required_terms=("pimenton",),
        excluding_terms=("pimiento",),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "pimienta": IngredientSpec(
        "pimienta",
        "aceites_condimentos",
        aliases=("pimienta", "pimienta negra", "pimienta blanca", "pimienta molida"),
        required_terms=("pimienta",),
        excluding_terms=("pimiento", "pimenton"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "canela": IngredientSpec(
        "canela",
        "aceites_condimentos",
        aliases=("canela", "canela molida", "canela en rama"),
        required_terms=("canela",),
        excluding_terms=("roll", "galleta", "cereal", "sabor"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "azafran": IngredientSpec(
        "azafran",
        "aceites_condimentos",
        aliases=("azafran", "azafran en hebras"),
        required_terms=("azafran",),
        excluding_terms=("colorante",),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "comino": IngredientSpec(
        "comino",
        "aceites_condimentos",
        aliases=("comino", "comino molido"),
        required_terms=("comino",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "laurel": IngredientSpec(
        "laurel",
        "aceites_condimentos",
        aliases=("laurel", "hoja de laurel", "hojas de laurel"),
        required_terms=("laurel",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "perejil": IngredientSpec(
        "perejil",
        "verduras",
        aliases=("perejil", "perejil fresco"),
        required_terms=("perejil",),
        excluding_terms=(),
        forbidden_forms=("seco", "deshidratado"),
        allowed_units=("g", "kg", "unit"),
    ),
    "vainilla": IngredientSpec(
        "vainilla",
        "aceites_condimentos",
        aliases=("vainilla", "azucar vainillado", "esencia de vainilla", "extracto de vainilla"),
        required_terms=("vainilla",),
        excluding_terms=("yogur", "helado", "sabor", "natillas"),
        forbidden_forms=(),
        allowed_units=("g", "kg", "ml", "l"),
    ),
    # --- Bakery / flours / grains --- #
    "harina": IngredientSpec(
        "harina",
        "panaderia_reposteria",
        # Generic wheat flour. Other-grain flours are DIFFERENT ingredients -> excluded.
        aliases=("harina", "harina de trigo", "harina de fuerza", "harina de reposteria"),
        required_terms=("harina",),
        excluding_terms=("maiz", "garbanzo", "almendra", "arroz", "avena", "coco", "centeno"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "maicena": IngredientSpec(
        "maicena",
        "panaderia_reposteria",
        aliases=("maicena", "harina fina de maiz", "almidon de maiz"),
        required_terms=("maicena",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "pan": IngredientSpec(
        "pan",
        "panaderia_reposteria",
        # Loaf/table bread. ``pan rallado`` (breadcrumbs) is a distinct ingredient -> excluded.
        aliases=("pan", "barra de pan", "pan de pueblo", "pan blanco"),
        required_terms=("pan",),
        excluding_terms=("rallado",),
        forbidden_forms=("rallado", "tostado", "molido"),
        allowed_units=("g", "kg", "unit"),
    ),
    "pan rallado": IngredientSpec(
        "pan rallado",
        "panaderia_reposteria",
        aliases=("pan rallado", "pan rallado con ajo y perejil"),
        required_terms=("pan", "rallado"),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "arroz": IngredientSpec(
        "arroz",
        "cereales_pasta_arroz",
        # 'arroz bomba' omitted: a premium paella rice, priced above commodity round/long rice.
        aliases=("arroz", "arroz redondo", "arroz largo"),
        required_terms=("arroz",),
        excluding_terms=("leche", "bebida", "vinagre", "harina", "tortitas"),
        forbidden_forms=("con leche",),
        allowed_units=("g", "kg"),
    ),
    "fideo": IngredientSpec(
        "fideo",
        "cereales_pasta_arroz",
        aliases=("fideo", "fideos", "fideo fino", "fideua"),
        required_terms=("fideo",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    # --- Dairy --- #
    "leche": IngredientSpec(
        "leche",
        "lacteos",
        # Generic cow milk (whole/semi/skimmed all qualify). Plant "milks" are excluded; the
        # specific ``leche_entera`` / ``leche_desnatada`` specs stay more precise (2 terms).
        aliases=("leche", "leche de vaca", "leche fresca"),
        required_terms=("leche",),
        excluding_terms=(
            "avena", "soja", "almendra", "coco", "arroz", "condensada", "evaporada",
            "polvo", "bebida", "merengada", "magnesia",
        ),
        forbidden_forms=("en polvo",),
        allowed_units=("l", "ml"),
        allergens=("milk",),
    ),
    "nata": IngredientSpec(
        "nata",
        "lacteos",
        aliases=("nata", "nata para cocinar", "nata liquida"),
        required_terms=("nata",),
        excluding_terms=("soja", "coco", "vegetal", "avena"),
        forbidden_forms=("montada", "spray"),
        allowed_units=("l", "ml"),
        allergens=("milk",),
    ),
    "mantequilla": IngredientSpec(
        "mantequilla",
        "lacteos",
        aliases=("mantequilla", "mantequilla sin sal"),
        required_terms=("mantequilla",),
        excluding_terms=("cacahuete", "mani", "vegetal", "soja"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
        allergens=("milk",),
    ),
    # --- Eggs --- #
    "huevo": IngredientSpec(
        "huevo",
        "huevos",
        aliases=("huevo", "huevos", "huevo fresco", "huevos frescos", "huevo l", "huevo m"),
        required_terms=("huevo",),
        excluding_terms=("hilado", "liquido", "pasteurizado", "codorniz", "chocolate", "kinder"),
        forbidden_forms=("en polvo", "deshidratado"),
        allowed_units=("unit", "g"),
        allergens=("egg",),
    ),
    # --- Meats --- #
    "pollo": IngredientSpec(
        "pollo",
        "carnes",
        aliases=("pollo", "pechuga de pollo", "muslo de pollo", "contramuslo de pollo"),
        required_terms=("pollo",),
        excluding_terms=("caldo", "sabor", "pastilla", "pienso"),
        forbidden_forms=("empanado", "nuggets", "rebozado", "precocinado"),
        allowed_units=("g", "kg", "unit"),
    ),
    "carne picada": IngredientSpec(
        "carne picada",
        "carnes",
        aliases=("carne picada", "carne picada mixta", "carne picada de ternera"),
        required_terms=("carne", "picada"),
        excluding_terms=("soja", "vegetal", "vegana"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "chorizo": IngredientSpec(
        "chorizo",
        "carnes",
        aliases=("chorizo", "chorizo fresco", "chorizo sarta"),
        required_terms=("chorizo",),
        excluding_terms=("sabor", "soja"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "morcilla": IngredientSpec(
        "morcilla",
        "carnes",
        aliases=("morcilla", "morcilla de arroz", "morcilla de cebolla"),
        required_terms=("morcilla",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "tocino": IngredientSpec(
        "tocino",
        "carnes",
        # 'panceta' intentionally omitted: a different cut, priced differently -> keep it distinct.
        aliases=("tocino", "tocino fresco"),
        required_terms=("tocino",),
        excluding_terms=("cielo",),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "jamón serrano": IngredientSpec(
        "jamón serrano",
        "carnes",
        aliases=("jamon serrano", "jamon curado"),
        required_terms=("jamon", "serrano"),
        excluding_terms=("york", "cocido", "dulce", "pavo"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "conejo": IngredientSpec(
        "conejo",
        "carnes",
        aliases=("conejo", "conejo troceado"),
        required_terms=("conejo",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg", "unit"),
    ),
    # --- Fish / seafood --- #
    "bacalao": IngredientSpec(
        "bacalao",
        "pescados",
        aliases=("bacalao", "bacalao desalado", "lomo de bacalao"),
        required_terms=("bacalao",),
        excluding_terms=("aceite", "higado", "pil"),
        forbidden_forms=("rebozado", "croqueta", "buñuelo"),
        allowed_units=("g", "kg"),
        allergens=("fish",),
    ),
    # --- Legumes --- #
    "garbanzo": IngredientSpec(
        "garbanzo",
        "legumbres",
        # 'garbanzo cocido' (canned, ready) omitted: priced differently from the dry legume.
        aliases=("garbanzo", "garbanzos"),
        required_terms=("garbanzo",),
        excluding_terms=("harina",),
        forbidden_forms=("frito", "tostado"),
        allowed_units=("g", "kg"),
    ),
    "alubia": IngredientSpec(
        "alubia",
        "legumbres",
        aliases=("alubia", "alubias", "alubia blanca", "judia blanca", "faba"),
        required_terms=("alubia",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    "judía verde": IngredientSpec(
        "judía verde",
        "verduras",
        aliases=("judia verde", "judias verdes"),
        required_terms=("judia", "verde"),
        excluding_terms=("blanca", "pinta"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    # --- Vegetables --- #
    "zanahoria": IngredientSpec(
        "zanahoria",
        "verduras",
        aliases=("zanahoria", "zanahorias"),
        required_terms=("zanahoria",),
        excluding_terms=(),
        forbidden_forms=("rallada", "deshidratada"),
        allowed_units=("g", "kg", "unit"),
    ),
    "pimiento": IngredientSpec(
        "pimiento",
        "verduras",
        # Generic fresh pepper. ``pimiento_rojo`` / ``pimiento verde`` stay specific (2 terms).
        aliases=("pimiento", "pimientos"),
        required_terms=("pimiento",),
        excluding_terms=("pimienta", "pimenton", "morron", "piquillo"),
        forbidden_forms=("en conserva", "en lata", "asado"),
        allowed_units=("g", "kg", "unit"),
    ),
    "pimiento_rojo": IngredientSpec(
        "pimiento_rojo",
        "verduras",
        aliases=("pimiento rojo", "pimientos rojos"),
        required_terms=("pimiento", "rojo"),
        excluding_terms=("verde", "amarillo", "pimienta", "pimenton"),
        forbidden_forms=("en conserva", "en lata"),
        allowed_units=("g", "kg", "unit"),
    ),
    "calabacin": IngredientSpec(
        "calabacin",
        "verduras",
        aliases=("calabacin", "calabacines"),
        required_terms=("calabacin",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("g", "kg", "unit"),
    ),
    "alcachofa": IngredientSpec(
        "alcachofa",
        "verduras",
        aliases=("alcachofa", "alcachofas"),
        required_terms=("alcachofa",),
        excluding_terms=(),
        forbidden_forms=("en conserva", "corazones en bote"),
        allowed_units=("g", "kg", "unit"),
    ),
    # --- Fruits / nuts --- #
    "limon": IngredientSpec(
        "limon",
        "frutas",
        aliases=("limon", "limones"),
        required_terms=("limon",),
        excluding_terms=("sabor", "refresco", "detergente", "friegasuelos"),
        forbidden_forms=("zumo", "en polvo"),
        allowed_units=("unit", "g", "kg"),
    ),
    "almendra": IngredientSpec(
        "almendra",
        "frutos_secos",
        aliases=("almendra", "almendras", "almendra molida", "almendra cruda"),
        required_terms=("almendra",),
        excluding_terms=("bebida", "leche", "turron", "sabor"),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
        allergens=("nuts",),
    ),
    "aceitunas": IngredientSpec(
        "aceitunas",
        "aceites_condimentos",
        aliases=("aceituna", "aceitunas", "aceitunas verdes", "aceitunas negras"),
        required_terms=("aceituna",),
        excluding_terms=("aceite",),
        forbidden_forms=(),
        allowed_units=("g", "kg"),
    ),
    # --- Broths --- #
    "caldo": IngredientSpec(
        "caldo",
        "caldos",
        aliases=("caldo", "caldo de pollo", "caldo de carne", "caldo casero"),
        required_terms=("caldo",),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("l", "ml", "g", "unit"),
    ),
    "caldo de pescado": IngredientSpec(
        "caldo de pescado",
        "caldos",
        aliases=("caldo de pescado", "fumet", "fumet de pescado"),
        required_terms=("caldo", "pescado"),
        excluding_terms=(),
        forbidden_forms=(),
        allowed_units=("l", "ml", "g"),
        allergens=("fish",),
    ),
    # --- Alcohol --- #
    "vino blanco": IngredientSpec(
        "vino blanco",
        "bebidas_alcohol",
        aliases=("vino blanco",),
        required_terms=("vino", "blanco"),
        excluding_terms=("tinto", "rosado", "vinagre"),
        forbidden_forms=("sin alcohol",),
        allowed_units=("l", "ml"),
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
    "aceites_condimentos": (
        "aceite", "sal ", "vinagre", "especia", "condimento", "aliño", "aceituna",
    ),
    "cereales_pasta_arroz": ("cereal", "avena", "arroz", "pasta", "copos", "fideo"),
    "panaderia_reposteria": ("panaderia", "reposteria", "pan", "harina", "azucar", "bolleria"),
    "huevos": ("huevo", "huevos"),
    "carnes": ("carne", "pollo", "cerdo", "ternera", "chorizo", "jamon", "embutido", "conejo"),
    "pescados": ("pescado", "marisco", "bacalao", "atun", "merluza", "gamba"),
    "legumbres": ("legumbre", "garbanzo", "lenteja", "alubia", "judia"),
    "frutos_secos": ("fruto seco", "frutos secos", "almendra", "nuez", "avellana"),
    "caldos": ("caldo", "fumet", "fondo"),
    "bebidas_alcohol": ("vino", "cerveza", "licor", "brandy", "coñac"),
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
