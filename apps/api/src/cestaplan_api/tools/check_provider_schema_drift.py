"""CLI: report schema drift for a provider (spec §L).

    python -m cestaplan_api.tools.check_provider_schema_drift --provider open-prices
    python -m cestaplan_api.tools.check_provider_schema_drift \
        --provider open-prices --sample new.json

With ``--sample`` it grades that schema against the stored latest; without it, prints the
latest stored version's metadata. A ``breaking``/``review_required`` status exits non-zero so
CI can block, and never auto-replaces the stored schema.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cestaplan_api.ingestion.providers.schema_registry import DEFAULT_BASE, check_drift, latest_meta


def run(provider: str, sample: str | None, base: Path) -> int:
    if sample is not None:
        new_schema = json.loads(Path(sample).read_text(encoding="utf-8"))
        result = check_drift(new_schema, provider, base=base)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return (
            0
            if result["compatibility_status"] in {"unchanged", "additive_compatible", "unavailable"}
            else 2
        )
    meta = latest_meta(base, provider)
    if meta is None:
        print(f"No hay esquema versionado para {provider!r}. Ejecuta fetch_provider_schema.")
        return 1
    print(json.dumps(meta.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Comprueba la deriva del esquema de un proveedor.")
    p.add_argument("--provider", required=True)
    p.add_argument("--sample", default=None)
    p.add_argument("--base", default=str(DEFAULT_BASE))
    a = p.parse_args()
    raise SystemExit(run(a.provider, a.sample, Path(a.base)))


if __name__ == "__main__":
    main()
