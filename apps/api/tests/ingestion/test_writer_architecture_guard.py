"""Architecture guard (spec §1): every writer that CONSTRUCTS a PriceObservation or a
PriceObservationOccurrence must be an explicitly-classified, allowlisted writer. A new
``PriceObservation(...)`` / ``PriceObservationOccurrence(...)`` anywhere else fails this test until
it is consciously classified — so no future staging writer silently bypasses the shared serialized
persistence (``record_price_fact``)."""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "cestaplan_api"

# Deliberate PriceObservation writers. Each is classified; adding one here is a conscious act.
#   staging     -> MUST route through record_price_fact (serialized, idempotent)
#   production  -> deliberate append-only history (out of scope for this PR)
#   recovery    -> admin/restore tooling under controlled, serial conditions
_PRICE_OBSERVATION_WRITERS = {
    "services/provider_sync.py",            # staging via record_price_fact + production append-only
    "services/targeted_discovery.py",       # staging via record_price_fact
    "ingestion/price_history.py",           # production append-only (deliberate)
    "ingestion/licensed_catalog.py",        # production append-only (deliberate)
    "tools/dedup_staging_observations.py",  # recovery: exact-restore reconstruction
}

_OCCURRENCE_WRITERS = {
    "services/observation_persistence.py",        # the canonical serialized writer
    "tools/backfill_observation_occurrences.py",  # one-time additive backfill (serial, controlled)
    "tools/dedup_staging_observations.py",        # relink / exact-restore
}


def _files_constructing(model_name: str) -> set[str]:
    """Every source file that instantiates ``model_name`` (a Call to the class), repo-relative."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == model_name
            ):
                found.add(str(path.relative_to(SRC)))
    return found


def test_no_unlisted_price_observation_writer() -> None:
    offenders = _files_constructing("PriceObservation") - _PRICE_OBSERVATION_WRITERS
    assert not offenders, (
        f"New PriceObservation writer(s) outside the allowlist: {sorted(offenders)}. "
        "A staging writer MUST go through record_price_fact; classify it consciously."
    )


def test_no_unlisted_occurrence_writer() -> None:
    offenders = _files_constructing("PriceObservationOccurrence") - _OCCURRENCE_WRITERS
    assert not offenders, (
        f"New PriceObservationOccurrence writer(s) outside the allowlist: {sorted(offenders)}."
    )


def test_allowlists_are_not_stale() -> None:
    # Guard the guard: every allowlisted writer still constructs the model (no dead entries).
    assert _files_constructing("PriceObservation") == _PRICE_OBSERVATION_WRITERS
    assert _files_constructing("PriceObservationOccurrence") == _OCCURRENCE_WRITERS
