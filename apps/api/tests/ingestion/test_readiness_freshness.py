"""Planner-readiness is never stale: the report is computed live from the DB (a prior zero never
persists after the catalogue changes) and carries ``fetched_at`` so a caller can show its age."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cestaplan_api.services.catalog_readiness import catalog_readiness_report
from cestaplan_api.tools.bootstrap_retailers import bootstrap


def test_report_has_fetched_at_iso_timestamp(db_session: Session) -> None:
    report = catalog_readiness_report(db_session)
    assert "fetched_at" in report
    assert isinstance(report["fetched_at"], str) and report["fetched_at"].startswith("20")


def test_report_reflects_new_chains_immediately(db_session: Session, monkeypatch) -> None:
    import cestaplan_api.tools.bootstrap_retailers as boot

    monkeypatch.setitem(boot.AUTHORIZED_CHAINS, "readiness_probe_chain", "Readiness Probe")

    before = int(catalog_readiness_report(db_session)["total_chains"])  # type: ignore[arg-type]
    bootstrap(db_session, ["readiness_probe_chain"])
    after = int(catalog_readiness_report(db_session)["total_chains"])  # type: ignore[arg-type]
    # No cached snapshot: the freshly-created chain shows up on the very next read.
    assert after == before + 1
