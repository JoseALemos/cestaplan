"""CLI: promote a sanitized candidate to a golden fixture (spec §N).

    python -m cestaplan_api.tools.promote_provider_fixture \
        --provider parsebot-dia \
        --candidate .local/provider-samples/parsebot-dia/sanitized.json \
        --output tests/fixtures/providers/parsebot-dia/v1.json \
        --reviewed-by ops --redistribution-status approved_for_repository \
        --source-rights-status commercial_use_allowed

Refuses to write unless rights are clear, a human reviewed it, and it holds no secrets/PII.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cestaplan_api.ingestion.providers.fixtures import FixtureManifest, can_version_fixture
from cestaplan_api.ingestion.providers.schema_tools import redact, structure_report


def run(args: argparse.Namespace) -> int:
    records = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        records = [records]
    # a sanitized candidate must already be secret-free; detect if redaction would change it
    has_secrets = redact(records) != records
    report = structure_report(records)
    manifest = FixtureManifest(
        provider=args.provider,
        fixture_version=args.fixture_version,
        schema_fingerprint=str(report["schema_fingerprint"]),
        manually_reviewed=bool(args.reviewed_by),
        reviewed_by=args.reviewed_by,
        redistribution_status=args.redistribution_status,
        source_rights_status=args.source_rights_status,
        contains_real_product_names=not args.no_real_names,
        contains_real_prices=not args.no_real_prices,
        contains_personal_data=args.contains_personal_data,
    )
    decision = can_version_fixture(manifest, has_secrets=has_secrets)
    if not decision.allowed:
        print("RECHAZADO: no puede versionarse la fixture:")
        for r in decision.reasons:
            print(f"  - {r}")
        return 1
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_suffix(".manifest.json").write_text(
        json.dumps(manifest.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Fixture dorada promovida: {out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Promueve una fixture sanitizada a dorada.")
    p.add_argument("--provider", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--fixture-version", default="v1")
    p.add_argument("--reviewed-by", default=None)
    p.add_argument("--redistribution-status", default="unknown")
    p.add_argument("--source-rights-status", default="unknown")
    p.add_argument("--no-real-names", action="store_true")
    p.add_argument("--no-real-prices", action="store_true")
    p.add_argument("--contains-personal-data", action="store_true")
    raise SystemExit(run(p.parse_args()))


if __name__ == "__main__":
    main()
