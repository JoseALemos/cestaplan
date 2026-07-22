"""CLI: report connector health (ConnectorState) per retailer.

    python -m cestaplan_api.jobs.connector_health
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from cestaplan_api.db import SessionLocal
from cestaplan_api.models import ConnectorState, Retailer


def run() -> int:
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(ConnectorState, Retailer)
                .join(Retailer, ConnectorState.retailer_id == Retailer.id)
                .order_by(Retailer.slug.asc(), ConnectorState.connector_version.asc())
            ).all()
        )

    print("CestaPlan — salud de conectores de ingesta")
    if not rows:
        print("  (sin estado de conectores registrado todavía)")
        return 0
    for state, retailer in rows:
        circuit = (
            state.circuit_open_until.isoformat()
            if state.circuit_open_until is not None
            else "-"
        )
        print(
            f"  {retailer.slug:<16} v{state.connector_version:<8} "
            f"estado={state.status:<20} "
            f"fallos={state.consecutive_failures} "
            f"circuito_abierto_hasta={circuit}"
        )
        if state.last_error:
            print(f"      último_error: {state.last_error[:120]}")
    return 0


def main() -> None:
    argparse.ArgumentParser(
        description="Reporta el estado de los conectores por retailer."
    ).parse_args()
    raise SystemExit(run())


if __name__ == "__main__":
    main()
