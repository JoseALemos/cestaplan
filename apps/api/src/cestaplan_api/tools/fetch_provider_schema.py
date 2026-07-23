"""CLI: fetch and version a provider's PUBLIC OpenAPI schema (spec §L).

    python -m cestaplan_api.tools.fetch_provider_schema --provider open-prices

Only public schemas are fetched (no credentials). The schema is stored as a NEW version under
``data/provider-schemas/<provider>/`` with metadata; an incompatible change is graded but never
overwrites the prior version. Not run automatically anywhere — an explicit operator command.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cestaplan_api.config import get_settings
from cestaplan_api.ingestion.providers.schema_registry import DEFAULT_BASE, store_schema


# Public OpenAPI endpoints (no auth). Only providers with a public schema are listed.
def _schema_url(provider: str) -> str | None:
    settings = get_settings()
    if provider == "open-prices":
        return settings.open_prices_base_url.rstrip("/") + "/openapi.json"
    return None


def run(provider: str, base: Path) -> int:
    url = _schema_url(provider)
    if url is None:
        print(f"{provider!r} no tiene un OpenAPI público conocido; usa fingerprints de muestra.")
        return 1
    try:
        resp = httpx.get(url, timeout=30, headers={"Accept": "application/json"})
        resp.raise_for_status()
        schema = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"No se pudo obtener el esquema ({type(exc).__name__}). Estado: unavailable.")
        return 1
    meta = store_schema(schema, provider, url, base=base, now=datetime.now(UTC))
    print(f"Esquema {provider} v{meta.version} guardado.")
    print(
        f"  sha256={meta.sha256} openapi={meta.openapi_version} status={meta.compatibility_status}"
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Descarga y versiona un OpenAPI público de proveedor.")
    p.add_argument("--provider", required=True)
    p.add_argument("--base", default=str(DEFAULT_BASE))
    a = p.parse_args()
    raise SystemExit(run(a.provider, Path(a.base)))


if __name__ == "__main__":
    main()
