"""CLI tools for §L/§M/§N — offline (no network, no credentials, no import).

Confirms capture refuses without credentials / on an unsafe path / over the limit and imports
nothing; drift reports 'no schema' before any fetch; synthetic generation writes a versionable
fixture; and promotion is rejected when rights are unknown but allowed once cleared.
"""

from __future__ import annotations

import json
from pathlib import Path

from cestaplan_api.tools import (
    capture_provider_sample,
    check_provider_schema_drift,
    generate_synthetic_provider_fixture,
    promote_provider_fixture,
)


# --- §M capture (credential forced empty so the test is env-independent) --- #
def test_capture_refuses_without_credentials(tmp_path: Path, capsys, monkeypatch) -> None:
    from cestaplan_api.config import get_settings

    monkeypatch.setenv("PARSE_BOT_API_KEY", "")  # override any ambient/.env value
    # Enable the chain so the refusal is the MISSING-KEY path (not the enable gate).
    monkeypatch.setenv("PARSE_BOT_ENABLED", "true")
    monkeypatch.setenv("PARSE_BOT_DIA_ENABLED", "true")
    get_settings.cache_clear()
    try:
        out = tmp_path / "raw.json"  # tmp is a safe (git-ignored-style) path
        rc = capture_provider_sample.run("parsebot-dia", 10, str(out), allow_versioned=False)
        assert rc == 1
        assert not out.exists()  # nothing written, nothing imported
        assert "PARSE_BOT_API_KEY" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_capture_rejects_versioned_path() -> None:
    rc = capture_provider_sample.run(
        "parsebot-dia", 10, "tests/fixtures/providers/x/raw.json", allow_versioned=False
    )
    assert rc == 1


def test_capture_rejects_over_limit(tmp_path: Path) -> None:
    rc = capture_provider_sample.run(
        "parsebot-dia", 25, str(tmp_path / "r.json"), allow_versioned=False
    )
    assert rc == 1


# --- §L drift ------------------------------------------------------------- #
def test_drift_reports_missing_schema(tmp_path: Path) -> None:
    rc = check_provider_schema_drift.run("open-prices", None, tmp_path)
    assert rc == 1


# --- §N synthetic + promote ------------------------------------------------ #
def test_generate_synthetic_writes_versionable_fixture(tmp_path: Path) -> None:
    report = {
        "schema_fingerprint": "f" * 64,
        "structure": {"name": "str", "price": "float", "id": "int"},
    }
    report_path = tmp_path / "schema-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "v1.synthetic.json"
    rc = generate_synthetic_provider_fixture.run(str(report_path), str(out), "parsebot-dia", "v1")
    assert rc == 0
    records = json.loads(out.read_text(encoding="utf-8"))
    assert records[0]["name"] == "SYNTHETIC"
    manifest = json.loads(out.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic_structure"] is True


def test_promote_rejected_when_rights_unknown(tmp_path: Path) -> None:
    candidate = tmp_path / "sanitized.json"
    candidate.write_text(json.dumps([{"name": "Leche", "price": 1.0}]), encoding="utf-8")
    out = tmp_path / "v1.json"
    import argparse

    args = argparse.Namespace(
        provider="parsebot-dia",
        candidate=str(candidate),
        output=str(out),
        fixture_version="v1",
        reviewed_by="ops",
        redistribution_status="unknown",  # <- blocks promotion
        source_rights_status="unknown",
        no_real_names=False,
        no_real_prices=False,
        contains_personal_data=False,
    )
    assert promote_provider_fixture.run(args) == 1
    assert not out.exists()

    # cleared rights -> allowed
    args.redistribution_status = "approved_for_repository"
    args.source_rights_status = "commercial_use_allowed"
    assert promote_provider_fixture.run(args) == 0
    assert out.exists()
