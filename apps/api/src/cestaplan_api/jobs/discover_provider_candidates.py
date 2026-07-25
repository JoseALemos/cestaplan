"""CLI: discover + map provider ingredient candidates into staging.

    python -m cestaplan_api.jobs.discover_provider_candidates --provider parsebot-dia
    python -m cestaplan_api.jobs.discover_provider_candidates --provider parsebot-alcampo \
        --ingredients leche_entera,aceite_oliva --limit 10

Review-only is the default and the ONLY mode a cloud deployment allows unless an operator has
explicitly set ``allow_deterministic_autoapproval=true``. Review-only never activates a mapping and
never creates a productive IngredientProductMapping: every match is a candidate awaiting human
review, with the machine's proposal preserved in the ``proposed_*`` fields.
"""

from __future__ import annotations

import argparse
import json

from cestaplan_api.config import get_settings
from cestaplan_api.db import SessionLocal
from cestaplan_api.services.targeted_discovery import (
    ApprovalMode,
    discover_and_map,
)

# A small, bounded priority vocabulary (matches the earlier targeted-coverage probes). Unknown keys
# are simply skipped, so this never fabricates ingredients.
_DEFAULT_INGREDIENTS = [
    "avena_copos",
    "tomate",
    "patata",
    "ajo",
    "sal",
    "espinaca",
    "aceite_oliva",
    "cebolla",
    "leche_entera",
    "platano",
]

_MODES = {
    "review-only": ApprovalMode.REVIEW_ONLY,
    "deterministic-autoapproval": ApprovalMode.DETERMINISTIC_AUTOAPPROVAL,
}


def run(
    provider_code: str,
    ingredient_keys: list[str],
    approval_mode: ApprovalMode,
    limit: int,
) -> int:
    settings = get_settings()
    # A cloud deployment NEVER auto-approves unless explicitly configured to.
    if (
        approval_mode is ApprovalMode.DETERMINISTIC_AUTOAPPROVAL
        and settings.deployment_mode == "cloud"
        and not settings.allow_deterministic_autoapproval
    ):
        print(
            "deterministic-autoapproval está bloqueado en cloud "
            "(allow_deterministic_autoapproval=false). Usa review-only."
        )
        return 2
    with SessionLocal() as db:
        report = discover_and_map(
            db,
            provider_code,
            ingredient_keys,
            per_query_limit=limit,
            approval_mode=approval_mode,
        )
        db.commit()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Descubre candidatos de mapping (staging).")
    parser.add_argument("--provider", required=True)
    parser.add_argument(
        "--approval-mode",
        choices=sorted(_MODES),
        default="review-only",  # safe default: never auto-approve implicitly
    )
    parser.add_argument("--ingredients", default=None, help="claves canónicas separadas por coma")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    keys = (
        [k.strip() for k in args.ingredients.split(",") if k.strip()]
        if args.ingredients
        else list(_DEFAULT_INGREDIENTS)
    )
    raise SystemExit(run(args.provider, keys, _MODES[args.approval_mode], args.limit))


if __name__ == "__main__":
    main()
