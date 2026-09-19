"""The generation worker's daily maintenance trigger for the Mercadona price refresh.

It must: never fire when the connector is off, fire at most once per 24h (throttled), pass the
cadence-aware production flags, and swallow any spawn failure so the job loop is never affected.
The subprocess itself is never launched here — ``subprocess.Popen`` is stubbed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import cestaplan_worker.main as m


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(mercadona_connector_enabled=enabled)


def _stub_popen(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: calls.append(list(cmd)))
    m._price_refresh_state["last_check"] = None  # reset module state
    return calls


def test_no_spawn_when_connector_disabled(monkeypatch) -> None:
    calls = _stub_popen(monkeypatch)
    m._maybe_refresh_prices(_settings(enabled=False), now=datetime(2026, 9, 19, tzinfo=UTC))
    assert calls == []


def test_spawns_cadence_aware_production_command(monkeypatch) -> None:
    calls = _stub_popen(monkeypatch)
    m._maybe_refresh_prices(_settings(), now=datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == [m.sys.executable, "-m", "cestaplan_api.jobs.sync_price_provider"]
    assert "--production" in cmd
    assert "apify-mercadona" in cmd
    i = cmd.index("--min-age-days")
    assert float(cmd[i + 1]) == float(m._PRICE_REFRESH_MIN_AGE_DAYS)


def test_throttled_to_once_per_day(monkeypatch) -> None:
    calls = _stub_popen(monkeypatch)
    t0 = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)
    m._maybe_refresh_prices(_settings(), now=t0)
    m._maybe_refresh_prices(_settings(), now=t0 + timedelta(hours=1))  # within 24h -> skip
    assert len(calls) == 1
    m._maybe_refresh_prices(_settings(), now=t0 + timedelta(hours=25))  # a day later -> fires
    assert len(calls) == 2


def test_spawn_failure_never_raises(monkeypatch) -> None:
    def _boom(cmd, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(m.subprocess, "Popen", _boom)
    m._price_refresh_state["last_check"] = None
    # Must not propagate — the job loop keeps running.
    m._maybe_refresh_prices(_settings(), now=datetime(2026, 9, 19, tzinfo=UTC))
