"""Deterministic ingredient-identity consolidation plan (pure logic, no I/O).

The recipe-costing engine matches a recipe ingredient to a buyable product by
``ingredient_id`` (an integer FK), never by name. Over time the ``ingredient``
table accumulated several rows for the *same* real ingredient under different
naming conventions — slug rows (``aceite_oliva``, ``pimiento_rojo``) living
alongside accented / plural / spaced rows (``azúcar``, ``aceitunas``,
``aceite de oliva``). Recipes point at the variants while the product mappings and
the ``_SPECS`` dictionary point at the slugs, so those recipes never resolve to a
price.

The owner's decision is *merge-to-slug + alias table*: the canonical slug form
survives, every variant is folded into it, and each variant name is recorded as an
alias so future imports re-attach to the survivor instead of forking a new row.

This module computes that merge plan **deterministically and idempotently**. It
performs no database access: it takes a snapshot of ingredient rows (``id`` +
``canonical_name``) and returns a :class:`ConsolidationPlan` the Alembic migration
executes. Re-running it on the same snapshot yields an identical plan; running it
again on an already-consolidated snapshot yields no merges.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from cestaplan_api.services.ingredient_dictionary import normalize as _normalize
from cestaplan_api.services.ingredient_dictionary import specs

# Connector words dropped when reducing a name to its grouping lemma. This is what makes
# "aceite de oliva" collapse onto "aceite_oliva" (the "de" is noise for identity).
_CONNECTORS = frozenset({"de", "del", "la", "el", "los", "las", "al", "a", "con", "y"})


@dataclass(frozen=True)
class IngredientMerge:
    """A single fold: ``old_id`` disappears, its FKs re-point to ``new_id``."""

    old_id: int
    new_id: int
    old_canonical_name: str
    new_canonical_name: str


@dataclass(frozen=True)
class IngredientAliasPlan:
    """A variant name (normalized) that should resolve to ``ingredient_id`` from now on."""

    alias_text: str
    ingredient_id: int


@dataclass(frozen=True)
class ConsolidationPlan:
    """The full deterministic plan: what to merge, what aliases to record, what to leave."""

    merges: tuple[IngredientMerge, ...]
    aliases: tuple[IngredientAliasPlan, ...]
    # Every group (survivor id + its variant ids) that needs NO structural change, i.e. a
    # group of a single row. Kept for observability / assertions; never acted upon.
    unmerged_groups: tuple[tuple[int, ...], ...]


# --------------------------------------------------------------------------- #
# Normalization helpers (pure, deterministic)
# --------------------------------------------------------------------------- #
def _slugify(name: str) -> str:
    """Canonical slug form: accent-free, lowercase, underscore-joined.

    ``"Aceite de Oliva"`` -> ``"aceite_de_oliva"``; ``"aceite_oliva"`` -> ``"aceite_oliva"``.
    A name already in this form is treated as the canonical/slug representative.
    """
    n = _normalize(name.replace("_", " "))
    return re.sub(r"\s+", "_", n).strip("_")


def _singularize(token: str) -> str:
    """Naive Spanish singularization: drop a trailing plural ``s`` on long tokens.

    Intentionally simple (per spec): only strips a trailing ``s`` from tokens longer than
    three characters. This folds vowel-plurals (``aceitunas`` -> ``aceituna``,
    ``lentejas`` -> ``lenteja``) without an over-eager ``-es`` rule that would mangle
    words like ``aceites`` -> ``aceite``. Invariant words (``cuscus`` -> ``cuscu``) fold
    consistently, which is harmless because grouping only compares lemmas to each other.
    """
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _lemma(name: str) -> str:
    """Reduce a name to its grouping lemma: normalized, connector-free, singularized.

    Two names that share a lemma are considered the same ingredient identity.
    """
    tokens = [t for t in _normalize(name.replace("_", " ")).split(" ") if t]
    kept = [_singularize(t) for t in tokens if t not in _CONNECTORS]
    return " ".join(kept)


def _build_spec_lemma_index() -> dict[str, str]:
    """Map every ``_SPECS`` alias/key lemma -> its spec key.

    Used as an *additional* equivalence hint so irregular aliases (``aove`` for olive oil,
    ``banana`` for ``platano``) group with their slug even when the naive lemma rules alone
    would not connect them.
    """
    index: dict[str, str] = {}
    for key, spec in specs().items():
        for text in (key, *spec.aliases):
            lemma = _lemma(text)
            # First spec to claim a lemma wins; specs do not overlap in practice.
            index.setdefault(lemma, key)
    return index


# --------------------------------------------------------------------------- #
# Union-find over ingredient ids
# --------------------------------------------------------------------------- #
class _DisjointSet:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic: keep the smaller id as the representative.
            hi, lo = (ra, rb) if ra > rb else (rb, ra)
            self._parent[hi] = lo


# --------------------------------------------------------------------------- #
# Plan construction
# --------------------------------------------------------------------------- #
def _pick_survivor(
    member_ids: list[int],
    name_by_id: dict[int, str],
    spec_keys: frozenset[str],
    active_mapping_ids: Collection[int],
) -> int:
    """Choose the surviving id by priority (spec §2):

    (a) canonical_name is exactly a ``_SPECS`` key, else
    (b) the ingredient has an active mapping, else
    (c) the name is already in canonical slug form (accent-free, ``_``-joined), else
    (d) the smallest id.
    Higher-priority signals win; ties break on the smallest id, so the result is stable.
    """

    def rank(ingredient_id: int) -> tuple[int, int, int, int]:
        name = name_by_id[ingredient_id]
        a = 1 if name in spec_keys else 0
        b = 1 if ingredient_id in active_mapping_ids else 0
        c = 1 if name == _slugify(name) else 0
        # Negate id so that, among equal (a, b, c), the SMALLEST id sorts highest.
        return (a, b, c, -ingredient_id)

    return max(member_ids, key=rank)


def build_consolidation_plan(
    ingredients: Iterable[tuple[int, str]],
    *,
    active_mapping_ingredient_ids: Collection[int] = (),
) -> ConsolidationPlan:
    """Compute the deterministic merge plan for a snapshot of ingredient rows.

    Args:
        ingredients: iterable of ``(id, canonical_name)`` pairs — the live ``ingredient``
            table snapshot.
        active_mapping_ingredient_ids: ids that are the target of an active product or
            provider mapping (survivor priority ``b``).

    Returns:
        A :class:`ConsolidationPlan`. ``merges`` and ``aliases`` are sorted by ``old_id`` /
        ``alias_text`` for reproducibility.
    """
    name_by_id: dict[int, str] = {}
    for ingredient_id, canonical_name in ingredients:
        name_by_id[int(ingredient_id)] = canonical_name

    active_ids = frozenset(int(i) for i in active_mapping_ingredient_ids)
    spec_keys = frozenset(specs().keys())
    spec_lemma_index = _build_spec_lemma_index()

    # 1. Union ingredients that share a lemma or resolve to the same _SPECS key.
    dsu = _DisjointSet()
    lemma_first: dict[str, int] = {}
    speckey_first: dict[str, int] = {}
    for ingredient_id in sorted(name_by_id):
        dsu.add(ingredient_id)
        lemma = _lemma(name_by_id[ingredient_id])
        first = lemma_first.setdefault(lemma, ingredient_id)
        dsu.union(ingredient_id, first)

        spec_key = spec_lemma_index.get(lemma)
        if spec_key is None:
            # Fall back to an exact-normalized-alias match for irregular aliases.
            spec_key = _match_spec_by_alias(name_by_id[ingredient_id])
        if spec_key is not None:
            anchor = speckey_first.setdefault(spec_key, ingredient_id)
            dsu.union(ingredient_id, anchor)

    # 2. Assemble connected components.
    groups: dict[int, list[int]] = {}
    for ingredient_id in name_by_id:
        groups.setdefault(dsu.find(ingredient_id), []).append(ingredient_id)

    merges: list[IngredientMerge] = []
    aliases: list[IngredientAliasPlan] = []
    unmerged: list[tuple[int, ...]] = []

    for member_ids in groups.values():
        member_ids.sort()
        if len(member_ids) == 1:
            unmerged.append(tuple(member_ids))
            continue

        survivor_id = _pick_survivor(member_ids, name_by_id, spec_keys, active_ids)
        survivor_name = name_by_id[survivor_id]
        survivor_alias = _normalize(survivor_name)

        for old_id in member_ids:
            if old_id == survivor_id:
                continue
            old_name = name_by_id[old_id]
            merges.append(
                IngredientMerge(
                    old_id=old_id,
                    new_id=survivor_id,
                    old_canonical_name=old_name,
                    new_canonical_name=survivor_name,
                )
            )
            alias_text = _normalize(old_name)
            if alias_text and alias_text != survivor_alias:
                aliases.append(
                    IngredientAliasPlan(alias_text=alias_text, ingredient_id=survivor_id)
                )

    # Deduplicate aliases (distinct variants may normalize identically) keeping the first.
    seen: set[str] = set()
    unique_aliases: list[IngredientAliasPlan] = []
    for alias in sorted(aliases, key=lambda a: a.alias_text):
        if alias.alias_text in seen:
            continue
        seen.add(alias.alias_text)
        unique_aliases.append(alias)

    return ConsolidationPlan(
        merges=tuple(sorted(merges, key=lambda m: m.old_id)),
        aliases=tuple(unique_aliases),
        unmerged_groups=tuple(sorted(unmerged)),
    )


def _match_spec_by_alias(name: str) -> str | None:
    """Return the spec key whose normalized alias/key exactly matches ``name``, else None."""
    target = _normalize(name)
    for key, spec in specs().items():
        if target == _normalize(key):
            return key
        if any(target == _normalize(alias) for alias in spec.aliases):
            return key
    return None


__all__ = [
    "ConsolidationPlan",
    "IngredientAliasPlan",
    "IngredientMerge",
    "build_consolidation_plan",
]
