"""A.3 two-layer observation metrics for the admin panel (spec §9).

Reports the price-fact / provenance split WITHOUT conflating them: many occurrences of one fact are
the SAME economic price confirmed repeatedly (by different crawls/parsers), NOT different prices.
It reuses the single shared fact identity via :func:`dedup_staging_observations.analyze` and the
candidate/explosion ratios via :func:`mapping_review.candidate_metrics`, so no metric re-implements
the fact identity or the ratios.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.models import (
    PriceObservation,
    PriceObservationOccurrence,
    ProviderIngredientMapping,
    Retailer,
)
from cestaplan_api.services.mapping_review import candidate_metrics
from cestaplan_api.tools import dedup_staging_observations as dedup


def _retailer_id(db: Session, provider_code: str) -> int | None:
    from cestaplan_api.ingestion.providers.onboarding import get_entry

    entry = get_entry(provider_code)
    slug = entry.retailer_slug if entry else provider_code
    return db.scalar(select(Retailer.id).where(Retailer.slug == slug))


def _quality_gate(m: dict[str, Any]) -> dict[str, Any]:
    """Derive a coarse gate from dedup pressure + ambiguous provenance + candidate explosion."""
    reasons: list[str] = []
    if m["duplicate_price_observations_by_fact_identity"] > 0:
        reasons.append("duplicate_price_observations_present")
    if m["occurrences_ambiguous_provenance"] > 0:
        reasons.append("ambiguous_provenance_present")
    if m.get("candidate_explosion_state") in ("warning", "critical"):
        reasons.append(f"candidate_{m['candidate_explosion_state']}")
    status = "ok"
    if m.get("candidate_explosion_state") == "critical":
        status = "critical"
    elif reasons:
        status = "warning"
    return {"status": status, "reasons": reasons}


def observation_metrics(db: Session, provider_code: str) -> dict[str, Any]:
    """The A.3 panel figures for one provider (spec §9).

    Distinguishes: unique price facts, provenance occurrences, staging observations, duplicates by
    fact identity, ambiguous provenance, plus the candidate side (vigentes/superseded/ratios).
    """
    retailer_id = _retailer_id(db, provider_code)
    analysis = dedup.analyze(db, provider_code)

    occurrences = 0
    ambiguous = 0
    if retailer_id is not None:
        occurrences = int(
            db.scalar(
                select(func.count())
                .select_from(PriceObservationOccurrence)
                .join(
                    PriceObservation,
                    PriceObservation.id == PriceObservationOccurrence.price_observation_id,
                )
                .where(
                    PriceObservation.retailer_id == retailer_id,
                    PriceObservation.staging_only.is_(True),
                )
            )
            or 0
        )
        ambiguous = int(
            db.scalar(
                select(func.count())
                .select_from(PriceObservationOccurrence)
                .join(
                    PriceObservation,
                    PriceObservation.id == PriceObservationOccurrence.price_observation_id,
                )
                .where(
                    PriceObservation.retailer_id == retailer_id,
                    PriceObservation.staging_only.is_(True),
                    PriceObservationOccurrence.crawl_run_id.is_(None),
                    PriceObservationOccurrence.raw_capture_id.is_(None),
                    PriceObservationOccurrence.source_id.is_(None),
                )
            )
            or 0
        )

    cand = candidate_metrics(db, provider_code)
    vigentes = superseded = 0
    if retailer_id is not None or provider_code:
        vigentes = int(
            db.scalar(
                select(func.count())
                .select_from(ProviderIngredientMapping)
                .where(
                    ProviderIngredientMapping.provider_code == provider_code,
                    ProviderIngredientMapping.superseded_at.is_(None),
                    ProviderIngredientMapping.mapping_status == "candidate",
                )
            )
            or 0
        )
        superseded = int(
            db.scalar(
                select(func.count())
                .select_from(ProviderIngredientMapping)
                .where(
                    ProviderIngredientMapping.provider_code == provider_code,
                    ProviderIngredientMapping.superseded_at.is_not(None),
                )
            )
            or 0
        )

    metrics: dict[str, Any] = {
        "provider_code": provider_code,
        "retailer_id": retailer_id,
        # --- price-fact layer (economic facts) ---
        "unique_price_facts": analysis["unique_fact_count"],
        "staging_observations": analysis["staging_observations"],
        # ALL duplicates sharing a fact identity...
        "duplicate_price_observations_by_fact_identity": analysis["total_duplicate_observations"],
        "duplicate_fact_groups": analysis["total_duplicate_fact_groups"],
        # ...and the safely-removable subset (verified provenance, no blocking FK).
        "removable_duplicate_observations": analysis["removable_price_observations"],
        # --- provenance layer (occurrences) ---
        "provenance_occurrences": occurrences,
        "occurrences_ambiguous_provenance": ambiguous,
        # --- candidate/mapping side (kept separate from prices) ---
        "candidatos_vigentes": vigentes,
        "superseded_candidates": superseded,
        "candidate_pair_ratio": cand.get("candidate_pair_ratio"),
        "multi_ingredient_product_ratio": cand.get("multi_ingredient_product_ratio"),
        "candidate_explosion_state": cand.get("explosion_state"),
        "note": (
            "Multiple occurrences of one fact are the SAME price confirmed repeatedly, "
            "never different prices."
        ),
    }
    metrics["quality_gate"] = _quality_gate(metrics)
    return metrics


__all__ = ["observation_metrics"]
