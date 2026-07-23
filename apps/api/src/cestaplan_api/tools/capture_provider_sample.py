"""CLI: safely capture a small real provider sample (spec §M).

    python -m cestaplan_api.tools.capture_provider_sample \
        --provider parsebot-dia --limit 10 \
        --output .local/provider-samples/parsebot-dia/raw.json

Reads credentials ONLY from environment/config, does a minimal bounded query, redacts secrets,
writes the raw+sanitized sample and a structure report to a git-ignored path, and imports
NOTHING (no products, no prices, no activation). Refuses a versioned output path unless
``--allow-sanitized-fixture-export`` is given for an already-sanitized fixture.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cestaplan_api.config import get_settings
from cestaplan_api.ingestion.providers.apify.client import ApifyClient
from cestaplan_api.ingestion.providers.parsebot import plans
from cestaplan_api.ingestion.providers.sample_capture import (
    build_capture_artifacts,
    path_is_safe,
)

_SUPPORTED = (
    "parsebot-dia",
    "parsebot-alcampo",
    "parsebot-carrefour",
    "parsebot-aldi",
    "parsebot-lidl",
    "parsebot-deza",
    "apify-mercadona",
)


def _fetch_raw(provider: str, limit: int, query: str) -> list[Any]:
    """Do a minimal, bounded capture using env credentials only (via the chain's plan)."""
    settings = get_settings()
    if provider.startswith("parsebot-"):
        # The per-chain capture plan owns the endpoint(s)/params/list extraction (plans.py).
        return plans.capture_records(provider, settings, limit=limit, query=query)
    if provider == "apify-mercadona":
        if not settings.apify_api_token:
            raise RuntimeError("APIFY_API_TOKEN no está configurada")
        client = ApifyClient(
            api_token=settings.apify_api_token,
            base_url=settings.apify_base_url,
            max_wait_seconds=settings.apify_max_wait_seconds,
            poll_interval_seconds=settings.apify_poll_interval_seconds,
        )
        run_id = client.start_run(settings.apify_mercadona_actor_id, {"maxItems": limit})
        run = client.wait_for_run(run_id)
        return client.get_dataset_items(str(run["defaultDatasetId"]), limit=limit)[:limit]
    raise RuntimeError(f"captura no soportada para {provider!r}")


def run(provider: str, limit: int, output: str, allow_versioned: bool, query: str = "leche") -> int:
    if provider not in _SUPPORTED:
        print(f"Proveedor no soportado para captura: {provider!r} (soportados: {_SUPPORTED})")
        return 1
    if limit > 10:
        print("El límite máximo de captura es 10 productos.")
        return 1
    ok, reason = path_is_safe(output, allow_versioned=allow_versioned)
    if not ok:
        print(
            f"Ruta insegura: {reason}. Usa una ruta en .local/ o --allow-sanitized-fixture-export."
        )
        return 1
    try:
        records = _fetch_raw(provider, limit, query)
    except RuntimeError as exc:
        print(f"No se capturó nada: {exc}")
        return 1

    artifacts = build_capture_artifacts(records, limit=limit)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifacts.raw_redacted, indent=2, ensure_ascii=False), "utf-8")
    sanitized = out.with_name("sanitized.json")
    sanitized.write_text(json.dumps(artifacts.sanitized, indent=2, ensure_ascii=False), "utf-8")
    report = out.with_name("schema-report.json")
    report.write_text(json.dumps(artifacts.report, indent=2, ensure_ascii=False), "utf-8")

    print("Captura completada (sin importar nada):")
    print(f"  muestra bruta (redactada): {out}")
    print(f"  muestra sanitizada:        {sanitized}")
    print(f"  informe de estructura:     {report}")
    print(f"  registros={artifacts.record_count}")
    print(f"  fingerprint={artifacts.schema_fingerprint}")
    print(f"  campos críticos: {artifacts.report['critical_fields']}")
    print(f"  campos a revisar: {artifacts.report['review_fields']}")
    print("  IMPORTACIÓN: ninguna. Proveedor NO activado.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Captura segura de una muestra de proveedor.")
    p.add_argument("--provider", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--output", required=True)
    p.add_argument("--allow-sanitized-fixture-export", action="store_true")
    p.add_argument("--query", default="leche", help="search term for search-based providers")
    a = p.parse_args()
    raise SystemExit(run(a.provider, a.limit, a.output, a.allow_sanitized_fixture_export, a.query))


if __name__ == "__main__":
    main()
