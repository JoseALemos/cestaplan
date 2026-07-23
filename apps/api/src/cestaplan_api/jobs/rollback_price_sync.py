"""CLI: logically roll back one price sync run (spec §T).

    python -m cestaplan_api.jobs.rollback_price_sync --run-id <UUID>

Reverses a run's observations (marking them rolled back, re-opening what they closed) without
any DELETE. Idempotent; pass --force to re-run an already-rolled-back sync.
"""

from __future__ import annotations

import argparse
import json
import uuid

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.price_rollback import rollback_sync


def run(run_id: str, force: bool) -> int:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        print(f"run-id no es un UUID válido: {run_id!r}")
        return 1
    with SessionLocal() as db:
        try:
            report = rollback_sync(db, run_uuid, actor_user_id=None, force=force)
        except ValueError as exc:
            print(str(exc))
            return 1
        db.commit()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Revierte lógicamente una sincronización.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.run_id, args.force))


if __name__ == "__main__":
    main()
