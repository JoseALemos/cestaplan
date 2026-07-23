"""CLI: build a synthetic golden fixture from a schema report (spec §N).

    python -m cestaplan_api.tools.generate_synthetic_provider_fixture \
        --from-report .local/provider-samples/parsebot-dia/schema-report.json \
        --output tests/fixtures/providers/parsebot-dia/v1.synthetic.json

Keeps structure and types, replaces every real value; marks synthetic_structure=true. Safe to
version (no real data). Writes the fixture and a sibling ``.manifest.json``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cestaplan_api.ingestion.providers.fixtures import build_synthetic_fixture


def run(from_report: str, output: str, provider: str, fixture_version: str) -> int:
    report = json.loads(Path(from_report).read_text(encoding="utf-8"))
    records, manifest = build_synthetic_fixture(report, provider, fixture_version=fixture_version)
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_suffix(".manifest.json").write_text(
        json.dumps(manifest.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Fixture sintética escrita: {out}")
    print(f"  registros={len(records)} fingerprint={manifest.schema_fingerprint} synthetic=True")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Genera una fixture sintética estructural.")
    p.add_argument("--from-report", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--fixture-version", default="v1")
    a = p.parse_args()
    raise SystemExit(run(a.from_report, a.output, a.provider, a.fixture_version))


if __name__ == "__main__":
    main()
