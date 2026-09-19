"""CLI flag wiring for the price-provider sync job (dry-run default; explicit production)."""

from __future__ import annotations

import sys

import pytest

from cestaplan_api.jobs import sync_price_provider as cli
from cestaplan_api.services.provider_sync import SyncMode


def _run_main(monkeypatch, argv: list[str]) -> SyncMode:
    captured: dict[str, SyncMode] = {}

    def fake_run(provider_code, retailer_slug, mode, limit, min_age_days=None):
        captured["mode"] = mode
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["sync_price_provider", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    return captured["mode"]


def test_default_is_dry_run(monkeypatch) -> None:
    assert _run_main(monkeypatch, ["--provider", "demo"]) is SyncMode.DRY_RUN


def test_staging_flag(monkeypatch) -> None:
    assert _run_main(monkeypatch, ["--provider", "demo", "--staging-import"]) is SyncMode.STAGING


def test_production_flag_selects_production(monkeypatch) -> None:
    assert _run_main(monkeypatch, ["--provider", "demo", "--production"]) is SyncMode.PRODUCTION


def test_production_wins_over_staging(monkeypatch) -> None:
    # Belt-and-braces: an explicit --production is never downgraded by another flag.
    mode = _run_main(monkeypatch, ["--provider", "demo", "--staging-import", "--production"])
    assert mode is SyncMode.PRODUCTION
