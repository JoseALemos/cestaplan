"""CLI: onboard every configured retailer provider (spec §3) — no secrets, no production.

    python -m cestaplan_api.tools.onboard_all_retailers --limit-per-provider 10 --continue-on-error

Walks the onboarding matrix independently per provider: checks configuration (without showing
secrets), does a bounded live fetch only where configured, upserts each provider's activation
row (rights stay under review, production never activated), and prints a final status matrix.
One chain's failure never blocks the others. It never runs a production import.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import select

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.ingestion.providers.contracts import ProductQuery
from cestaplan_api.ingestion.providers.onboarding import (
    RETAILER_MATRIX,
    MatrixEntry,
    OnboardingMatrix,
    ProviderOnboardingReport,
    config_status,
    measure_coverage,
    upsert_activation,
)
from cestaplan_api.ingestion.providers.registry import registry
from cestaplan_api.models import Retailer


def _status_for(entry: MatrixEntry, captured: int) -> str:
    role = entry.intended_role
    if role == "development_fallback":
        return "completed"
    if role == "complementary":
        return "partial"
    if "experimental" in role:
        return "experimental"
    if role == "partial_offers":
        return "partial"
    if role.startswith("dense_catalog"):
        return "completed" if captured > 0 else "partial"
    return "completed"


def _ensure_retailer(db, slug: str) -> None:
    if slug in ("open_prices",):  # not a real store chain
        return
    exists = db.execute(select(Retailer.id).where(Retailer.slug == slug)).first()
    if exists is None:
        db.add(
            Retailer(slug=slug, name=slug.capitalize(), adapter_key="provider", is_synthetic=False)
        )
        db.flush()


def _onboard_one(db, entry: MatrixEntry, settings, limit: int) -> ProviderOnboardingReport:
    cfg = config_status(entry, settings)
    report = ProviderOnboardingReport(
        provider_code=entry.provider_code,
        retailer_slug=entry.retailer_slug,
        intended_role=entry.intended_role,
        intended_catalog_scope=entry.intended_catalog_scope,
        configured=cfg.configured,
        rights="under_review",
    )
    _ensure_retailer(db, entry.retailer_slug)

    if not cfg.configured:
        report.status = cfg.blocked_reason or "blocked_by_missing_configuration"
        report.mapper_status = "blocked"
        upsert_activation(
            db, entry, now=datetime.now(UTC), transport_status="down", mapper_status="blocked"
        )
        return report

    if not registry.has(entry.provider_code):
        report.status = "blocked_by_missing_schema"
        report.mapper_status = "blocked"
        upsert_activation(
            db,
            entry,
            now=datetime.now(UTC),
            transport_status="operational",
            mapper_status="blocked",
        )
        return report

    provider = registry.get(entry.provider_code)
    try:
        products = list(provider.iterate_products(ProductQuery(max_products=limit)))
    except Exception as exc:
        report.status = "failed"
        report.mapper_status = "error"
        report.error = type(exc).__name__
        upsert_activation(
            db, entry, now=datetime.now(UTC), transport_status="degraded", mapper_status="error"
        )
        return report

    coverage = measure_coverage(
        products,
        captured=len(products),
        limit=limit,
        supports_full_catalog=provider.supports_full_catalog(),
        supports_store_scope=provider.supports_store_scope(),
    )
    report.captured = len(products)
    report.mapper_status = "verified" if products else "unknown"
    report.observed_catalog_scope = coverage.observed_catalog_scope
    report.costing_eligibility = coverage.costing_eligibility
    report.status = _status_for(entry, len(products))
    upsert_activation(
        db,
        entry,
        now=datetime.now(UTC),
        transport_status="operational",
        mapper_status=report.mapper_status,
        # Data quality follows the OBSERVED costing eligibility, never the declared intent.
        data_quality_status="accepted" if coverage.costing_eligibility == "sufficient" else (
            "degraded" if coverage.observed_catalog_scope != "unknown" else "insufficient"
        ),
        coverage=coverage,
    )
    return report


def run(limit: int, continue_on_error: bool) -> int:
    settings = get_settings()
    matrix = OnboardingMatrix(generated_at=datetime.now(UTC).isoformat())
    with SessionLocal() as db:
        for entry in RETAILER_MATRIX:
            try:
                matrix.rows.append(_onboard_one(db, entry, settings, limit))
            except Exception as exc:
                if not continue_on_error:
                    raise
                matrix.rows.append(
                    ProviderOnboardingReport(
                        entry.provider_code,
                        entry.retailer_slug,
                        entry.intended_role,
                        entry.intended_catalog_scope,
                        configured=False,
                        status="failed",
                        error=type(exc).__name__,
                    )
                )
        db.commit()

    print(json.dumps(matrix.as_dict(), indent=2, ensure_ascii=False))
    header = (
        "\n| Cadena | Proveedor | Configurado | Estado | Capturados | "
        "Scope declarado | Scope observado | Costeable | Derechos |"
    )
    print(header)
    print("|---|---|---|---|---|---|---|---|---|")
    for r in matrix.rows:
        cap = "-" if r.captured is None else str(r.captured)
        print(
            f"| {r.retailer_slug} | {r.provider_code} | {r.configured} | {r.status} | "
            f"{cap} | {r.intended_catalog_scope} | {r.observed_catalog_scope} | "
            f"{r.costing_eligibility} | {r.rights} |"
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Onboard todos los proveedores de retailers.")
    p.add_argument("--limit-per-provider", type=int, default=10)
    p.add_argument("--continue-on-error", action="store_true")
    a = p.parse_args()
    raise SystemExit(run(a.limit_per_provider, a.continue_on_error))


if __name__ == "__main__":
    main()
