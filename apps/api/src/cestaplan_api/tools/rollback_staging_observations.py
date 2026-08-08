"""CLI: logically roll back STAGING price observations of one retailer (spec §T).

    python -m cestaplan_api.tools.rollback_staging_observations --retailer <slug>
    python -m cestaplan_api.tools.rollback_staging_observations \
        --retailer <slug> --scope unknown --only-orphan --apply

Reaches staging observations the per-run rollback cannot: rows discovery created with
``crawl_run_id=None`` (``--only-orphan``). Never a DELETE — matches are marked rolled back and
their validity closed. ``--apply`` is required to write (and commit); without it the run is a
dry-run that reports what WOULD change. The query is always constrained to ``staging_only`` — it
can never touch productive history.
"""

from __future__ import annotations

import argparse
import json

from cestaplan_api.db import SessionLocal
from cestaplan_api.services.price_rollback import rollback_staging_observations


def run(
    retailer: str,
    *,
    scope: str | None,
    only_orphan: bool,
    apply: bool,
) -> int:
    with SessionLocal() as db:
        try:
            report = rollback_staging_observations(
                db,
                retailer_slug=retailer,
                scope=scope,
                only_orphan=only_orphan,
                apply=apply,
                actor_user_id=None,
            )
        except ValueError as exc:
            print(str(exc))
            return 1
        if apply:
            db.commit()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Revierte lógicamente observaciones de precio de STAGING."
    )
    parser.add_argument("--retailer", required=True)
    parser.add_argument("--scope")
    parser.add_argument("--only-orphan", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        run(
            args.retailer,
            scope=args.scope,
            only_orphan=args.only_orphan,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    main()
