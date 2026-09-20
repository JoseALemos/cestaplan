"""Normalize recipe ``required_equipment`` to canonical :data:`KnownEquipment` codes.

Seed recipes already use the canonical English codes, but recipes imported from
``data/recipes/*.json`` declared free-text Spanish terms (``olla``, ``sarten``, even
``horno`` = ``oven``). The engine filters candidates by a strict subset check
(``recipe.required_equipment <= available_equipment``), so any non-canonical term never
matches and silently excludes the recipe from EVERY plan. This module maps the known
terms to codes and drops mere utensils (a knife or bowl is not a conditioning appliance),
so a household that declares its appliances can actually cook these recipes.

``unmapped_terms`` surfaces anything that is neither canonical, mapped, nor a recognised
utensil — so a data fix fails loud instead of silently dropping a real appliance.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import get_args

from cestaplan_api.schemas.household import KnownEquipment

_KNOWN: frozenset[str] = frozenset(get_args(KnownEquipment))

# Free-text term (lowercased) -> canonical KnownEquipment code.
_ES_TO_CODE: dict[str, str] = {
    # Ollas y cazuelas -> pot
    "olla": "pot",
    "cazo": "pot",
    "cazuela": "pot",
    "cacerola": "pot",
    "puchero": "pot",
    # Sartén / wok se usan sobre el fuego -> stovetop
    "sarten": "stovetop",
    "sartén": "stovetop",
    "wok": "stovetop",
    # Plancha -> griddle (código propio "Plancha")
    "plancha": "griddle",
    # Horno
    "horno": "oven",
    "fuente_horno": "oven",
    "fuente de horno": "oven",
    "bandeja_horno": "oven",
    "bandeja de horno": "oven",
    # Electrodomésticos
    "batidora": "blender",
    "licuadora": "blender",
    "tostadora": "toaster",
    "microondas": "microwave",
    "pasapures": "food_processor",
    "pasapurés": "food_processor",
    "robot": "food_processor",
    "robot_cocina": "food_processor",
    "robot de cocina": "food_processor",
    "procesador": "food_processor",
    "picadora": "food_processor",
    "freidora": "airfryer",
    "freidora_aire": "airfryer",
    "freidora de aire": "airfryer",
    "olla_presion": "pressure_cooker",
    "olla a presión": "pressure_cooker",
    "olla exprés": "pressure_cooker",
    "barbacoa": "barbacoa",
    "parrilla": "barbacoa",
}

# Hand utensils that everyone has: NOT conditioning appliances, so they impose no
# requirement and are dropped rather than mapped.
_NON_EQUIPMENT: frozenset[str] = frozenset(
    {
        "cuchillo",
        "bol",
        "bowl",
        "vaso",
        "cuchara",
        "cucharon",
        "cucharón",
        "batidor",
        "plato",
        "tenedor",
        "colador",
        "escurridor",
        "rallador",
        "tabla",
        "espatula",
        "espátula",
        "fuente",
    }
)


def _canonical(term: str) -> str | None:
    """Canonical code for a raw term, or ``None`` if it imposes no requirement."""
    key = (term or "").strip().lower()
    if key in _KNOWN:
        return key
    return _ES_TO_CODE.get(key)


def normalize_equipment(raw: Iterable[str] | None) -> list[str]:
    """Canonical, de-duplicated, order-stable equipment codes for ``raw``.

    Utensils and unknown terms impose no requirement and are dropped (the safe
    direction: fewer requirements, never a phantom one).
    """
    codes: list[str] = []
    for term in raw or []:
        code = _canonical(term)
        if code is not None and code not in codes:
            codes.append(code)
    return codes


def unmapped_terms(raw: Iterable[str] | None) -> list[str]:
    """Terms that are neither canonical, mapped, nor a recognised utensil.

    A non-empty result means the mapping is missing an entry: callers should surface it
    rather than silently drop what might be a real appliance requirement.
    """
    out: list[str] = []
    for term in raw or []:
        key = (term or "").strip().lower()
        if key and key not in _KNOWN and key not in _ES_TO_CODE and key not in _NON_EQUIPMENT:
            out.append(key)
    return out
