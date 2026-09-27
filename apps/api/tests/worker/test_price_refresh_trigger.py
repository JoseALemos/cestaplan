"""El disparo de mantenimiento del worker para el refresco de precios de Mercadona (A6).

Debe: no disparar nunca con el conector apagado, decidir la CADENCIA contra la BD (antigüedad del
precio de producción de Mercadona, durable frente a reinicios), limitar la frecuencia de la CONSULTA
a BD, pasar los flags de producción, y tragarse cualquier fallo de spawn para no afectar al bucle.
El subproceso real nunca se lanza aquí — ``subprocess.Popen`` está stubbeado.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

import cestaplan_worker.main as m
from tests.fixtures.provider_scenarios import (
    seed_test_catalog_product,
    seed_test_price_observation,
    seed_test_retailer,
)


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(mercadona_connector_enabled=enabled)


@contextmanager
def _dummy_session():
    yield object()  # no se usa: _mercadona_refresh_due está stubbeado en los tests de trigger


def _stub_trigger(monkeypatch, *, due: bool = True) -> list[list[str]]:
    """Stubbea Popen, la sesión de BD y la cadencia; resetea el estado del módulo."""
    calls: list[list[str]] = []
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: calls.append(list(cmd)))
    monkeypatch.setattr(m, "SessionLocal", lambda: _dummy_session())
    monkeypatch.setattr(m, "_mercadona_refresh_due", lambda db, now, *, min_age_days: due)
    m._price_refresh_state["last_db_check"] = None
    return calls


# --------------------------------------------------------------------------- #
# Lógica del trigger (sin BD, cadencia stubbeada)
# --------------------------------------------------------------------------- #
def test_no_spawn_when_connector_disabled(monkeypatch) -> None:
    calls = _stub_trigger(monkeypatch)
    m._maybe_refresh_prices(_settings(enabled=False), now=datetime(2026, 9, 19, tzinfo=UTC))
    assert calls == []


def test_spawns_cadence_aware_production_command_when_due(monkeypatch) -> None:
    calls = _stub_trigger(monkeypatch, due=True)
    m._maybe_refresh_prices(_settings(), now=datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == [m.sys.executable, "-m", "cestaplan_api.jobs.sync_price_provider"]
    assert "--production" in cmd
    assert "apify-mercadona" in cmd
    i = cmd.index("--min-age-days")
    assert float(cmd[i + 1]) == float(m._PRICE_REFRESH_MIN_AGE_DAYS)


def test_no_spawn_when_not_due(monkeypatch) -> None:
    calls = _stub_trigger(monkeypatch, due=False)
    m._maybe_refresh_prices(_settings(), now=datetime(2026, 9, 19, 6, 0, tzinfo=UTC))
    assert calls == []


def test_db_check_throttled_to_once_per_day(monkeypatch) -> None:
    calls = _stub_trigger(monkeypatch, due=True)
    t0 = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)
    m._maybe_refresh_prices(_settings(), now=t0)
    m._maybe_refresh_prices(_settings(), now=t0 + timedelta(hours=1))  # < 24h -> ni consulta
    assert len(calls) == 1
    m._maybe_refresh_prices(_settings(), now=t0 + timedelta(hours=25))  # un día después -> dispara
    assert len(calls) == 2


def test_spawn_failure_never_raises(monkeypatch) -> None:
    _stub_trigger(monkeypatch, due=True)

    def _boom(cmd, **kw):
        raise OSError("cannot spawn")

    monkeypatch.setattr(m.subprocess, "Popen", _boom)
    # No debe propagar — el bucle de jobs sigue vivo.
    m._maybe_refresh_prices(_settings(), now=datetime(2026, 9, 19, tzinfo=UTC))


# --------------------------------------------------------------------------- #
# Ancla DURABLE de la cadencia (contra BD real)
# --------------------------------------------------------------------------- #
def test_refresh_due_true_when_no_mercadona_prices(db_session: Session) -> None:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    # Sin ninguna observación de Mercadona en producción -> toca refrescar.
    assert m._mercadona_refresh_due(db_session, now, min_age_days=28) is True


def test_refresh_not_due_with_fresh_production_price(db_session: Session) -> None:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    retailer = seed_test_retailer(db_session, "mercadona", name="Mercadona")
    _p, variant = seed_test_catalog_product(
        db_session, retailer, "MERCA-FRESH", name="Leche", price=None
    )
    seed_test_price_observation(
        db_session, variant, amount="1.10", staging=False, now=now - timedelta(days=2)
    )
    # Precio de producción de hace 2 días (< 28) -> NO toca.
    assert m._mercadona_refresh_due(db_session, now, min_age_days=28) is False


def test_refresh_due_when_production_price_is_stale(db_session: Session) -> None:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    retailer = seed_test_retailer(db_session, "mercadona", name="Mercadona")
    _p, variant = seed_test_catalog_product(
        db_session, retailer, "MERCA-STALE", name="Pan", price=None
    )
    # Producción antigua (40 días) + una staging RECIENTE que debe IGNORARSE.
    seed_test_price_observation(
        db_session, variant, amount="1.00", staging=False, now=now - timedelta(days=40)
    )
    seed_test_price_observation(
        db_session, variant, amount="1.05", staging=True, now=now
    )
    assert m._mercadona_refresh_due(db_session, now, min_age_days=28) is True
