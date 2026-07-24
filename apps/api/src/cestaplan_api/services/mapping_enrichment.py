"""Single-call detail enrichment for a mapping candidate (spec §6/§7).

Exactly ONE bounded provider-detail call per admin request, rate-limited + daily-budgeted, fully
audited. Never stores raw payloads or secrets — only sanitized derived fields go into
``evidence_json`` (the previous evidence is preserved). Enrichment recomputes the deterministic
scores/costing signal and may auto-approve ONLY when the deterministic rules allow AND the
provider's candidate explosion is not critical. It never touches production.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.models import ProviderIngredientMapping, ProviderUsage
from cestaplan_api.services import mapping_review as mr
from cestaplan_api.services.ingredient_dictionary import (
    classify_mapping,
    normalize_provider_category,
)

# Sanitised detail fields we keep (never headers/cookies/tokens/raw bodies).
_ALLOWED_DETAIL = (
    "category",
    "description",
    "net_content",
    "quantity",
    "unit",
    "price",
    "unit_price",
    "unit_price_unit",
    "availability",
    "ingredients",
    "allergens",
    "format",
    "storage",
    "sold_by_weight",
    "min_increment",
    "scope",
    "brand",
)
# Detail endpoint per provider (already-discovered contracts); others are unavailable.
# Alcampo's endpoint is `get_product_details` (plural) per the discovered API spec.
_DETAIL_ENDPOINT = {
    "parsebot-alcampo": "/get_product_details",
    "parsebot-carrefour": "/get_product_detail",
    "parsebot-dia": "/get_product_detail",
}

# Costing-relevant detail fields per provider + the fingerprint of that observed shape. An
# UNKNOWN fingerprint blocks ONLY enrichment (never the search mapper / whole provider).
_DETAIL_CONTRACT_FIELDS = {
    "parsebot-alcampo": ("name", "packSizeDescription", "type", "price", "unitPrice"),
}
_DETAIL_CONTRACT_FINGERPRINT = {
    "parsebot-alcampo": "c9b95670f56909b83802cecf30fa718771f83fedaf73ca3457b74ff6f134fe3c",
}
_UNIT_PRICE_UNITS = {
    "fop.price.per.kg": "kg",
    "fop.price.per.kilo": "kg",
    "per_kilo": "kg",
    "fop.price.per.litre": "l",
    "per_litre": "l",
    "kg": "kg",
    "l": "l",
    "g": "g",
    "ml": "ml",
}
_SIZE_UNIT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(ml|cl|l|kg|g)\b", re.I)


def _typed_skeleton(value: Any) -> Any:
    """Presence+type skeleton of a payload (values discarded) for a stable contract fingerprint."""
    if isinstance(value, dict):
        return {k: _typed_skeleton(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_typed_skeleton(value[0])] if value else []
    return type(value).__name__


def detail_contract_fingerprint(inner: dict[str, Any], fields: tuple[str, ...]) -> str:
    """SHA-256 of the typed skeleton of the costing-relevant subset — never any value."""
    subset = {k: _typed_skeleton(inner[k]) for k in fields if k in inner}
    return hashlib.sha256(json.dumps(subset, sort_keys=True).encode()).hexdigest()


def _adapt_alcampo_detail(inner: dict[str, Any]) -> dict[str, Any]:
    """Translate Alcampo's raw detail fields into our sanitized vocabulary (pinned fingerprint)."""
    out: dict[str, Any] = {}
    if inner.get("brand"):
        out["brand"] = str(inner["brand"])
    cats = inner.get("categoryPath")
    if isinstance(cats, list) and cats:
        code = normalize_provider_category(str(cats[-1]))
        if code:
            out["category"] = code
    size = inner.get("packSizeDescription")
    if size:
        out["net_content"] = str(size)
        m = _SIZE_UNIT_RE.search(str(size))
        if m:
            out["unit"] = m.group(1).lower()
    amount = (inner.get("price") or {}).get("amount")
    if amount is not None:
        out["price"] = str(amount)
    up = inner.get("unitPrice") or {}
    up_amount = (up.get("price") or {}).get("amount")
    if up_amount is not None:
        out["unit_price"] = str(up_amount)
        upu = _UNIT_PRICE_UNITS.get(str(up.get("unit") or up.get("unitName") or "").lower())
        if upu:
            out["unit_price_unit"] = upu
    if inner.get("ingredients"):
        out["ingredients"] = str(inner["ingredients"])
    return out


_DETAIL_ADAPTERS = {"parsebot-alcampo": _adapt_alcampo_detail}


class DetailFetcher(Protocol):
    def __call__(
        self, provider_code: str, external_product_id: str, settings: Settings
    ) -> dict[str, Any]: ...


class EnrichmentUnavailable(Exception):
    """The provider has no usable detail endpoint."""


class EnrichmentFailed(Exception):
    def __init__(self, category: str, detail: str = "") -> None:
        super().__init__(detail)
        self.category = category


@dataclass(slots=True)
class BudgetState:
    used: int
    budget: int

    @property
    def exceeded(self) -> bool:
        return self.used >= self.budget


def _budget(db: Session, provider_code: str, settings: Settings, now: datetime) -> BudgetState:
    since = now - timedelta(days=1)
    used = int(
        db.execute(
            select(func.count(ProviderUsage.id)).where(
                ProviderUsage.provider == provider_code,
                ProviderUsage.operation == "enrichment",
                ProviderUsage.started_at >= since,
            )
        ).scalar_one()
    )
    return BudgetState(used=used, budget=settings.enrichment_daily_budget)


def _rate_limited(row: ProviderIngredientMapping, settings: Settings, now: datetime) -> bool:
    if row.enrichment_requested_at is None:
        return False
    return (now - row.enrichment_requested_at) < timedelta(
        seconds=settings.enrichment_min_seconds_between
    )


def _default_detail_fetcher(
    provider_code: str, external_product_id: str, settings: Settings
) -> dict[str, Any]:
    """One real detail call (never used in tests — tests inject a fake fetcher)."""
    endpoint = _DETAIL_ENDPOINT.get(provider_code)
    if endpoint is None:
        raise EnrichmentUnavailable(provider_code)
    from cestaplan_api.ingestion.providers.parsebot import plans
    from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient

    base = getattr(settings, plans.base_url_attr(provider_code), "") or ""
    if not settings.parse_bot_api_key or not base:
        raise EnrichmentUnavailable(provider_code)
    client = ParseBotClient(
        base_url=base,
        api_key=settings.parse_bot_api_key,
        timeout=settings.enrichment_timeout_seconds,
        max_retries=1,
    )
    try:
        data = client.get_json(endpoint, {"product_id": external_product_id})
    except Exception as exc:  # transport/HTTP/non-JSON already typed by the client
        raise EnrichmentFailed("transport", type(exc).__name__) from exc
    inner = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(inner, dict):
        raise EnrichmentFailed("non_json_or_shape")
    # Pin the detail contract by fingerprint: an unknown shape blocks ONLY enrichment.
    expected = _DETAIL_CONTRACT_FINGERPRINT.get(provider_code)
    if expected is not None:
        fields = _DETAIL_CONTRACT_FIELDS.get(provider_code, ())
        if detail_contract_fingerprint(inner, fields) != expected:
            raise EnrichmentFailed("detail_contract_unknown")
    adapter = _DETAIL_ADAPTERS.get(provider_code)
    return adapter(inner) if adapter is not None else inner


def _sanitize(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep ONLY whitelisted, non-sensitive derived fields."""
    return {k: detail[k] for k in _ALLOWED_DETAIL if k in detail and detail[k] is not None}


def enrich(
    db: Session,
    mapping_id: int,
    *,
    requested_by: int,
    settings: Settings | None = None,
    now: datetime | None = None,
    detail_fetcher: DetailFetcher | None = None,
) -> ProviderIngredientMapping:
    """Enrich one candidate from the provider detail endpoint (single call, audited)."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    fetch = detail_fetcher or _default_detail_fetcher
    row = db.get(ProviderIngredientMapping, mapping_id)
    if row is None:
        raise mr.ReviewError(f"mapping {mapping_id} not found")

    # Rate-limit against the PREVIOUS request time (before overwriting it).
    if _rate_limited(row, settings, now):
        row.enrichment_status = "rate_limited"
        db.flush()
        return row
    row.enrichment_requested_at = now
    row.enrichment_requested_by = requested_by
    row.provider_endpoint = _DETAIL_ENDPOINT.get(row.provider_code)
    if _budget(db, row.provider_code, settings, now).exceeded:
        row.enrichment_status = "budget_exceeded"
        db.flush()
        return row
    if row.provider_code not in _DETAIL_ENDPOINT:
        row.enrichment_status = "unavailable"
        db.flush()
        return row

    row.enrichment_status = "pending"
    db.flush()
    try:
        raw = fetch(row.provider_code, row.external_product_id, settings)
    except EnrichmentUnavailable:
        row.enrichment_status = "unavailable"
        db.flush()
        return row
    except EnrichmentFailed as exc:
        row.enrichment_status = "failed"
        row.enrichment_error_category = exc.category
        db.flush()
        return row

    derived = _sanitize(raw)
    previous = dict(row.evidence_json or {})
    # Recompute the deterministic classification with the enriched category/unit — a lexical
    # match alone still never auto-approves.
    cand = classify_mapping(
        row.canonical_ingredient_key,
        product_name=str(previous.get("product_name", "")),
        brand=derived.get("brand"),
        category_code=derived.get("category"),
        net_content_unit=derived.get("unit"),
    )
    row.confidence_score = cand.confidence
    row.category_score = cand.category_score
    row.lexical_score = cand.lexical_score
    row.unit_compatibility = cand.unit_compatibility
    row.preparation_compatibility = cand.preparation_compatibility
    row.enrichment_status = "completed"
    row.enrichment_completed_at = now
    row.evidence_json = {
        **previous,
        "previous_evidence": {k: v for k, v in previous.items() if k != "previous_evidence"},
        "enriched": derived,  # sanitized derived fields only
        "enriched_classification": cand.as_dict(),
    }
    # Auto-approve ONLY on a deterministic rule AND when the provider is not explosion-blocked.
    allowed = bool(mr.candidate_metrics(db, row.provider_code).get("auto_approval_allowed", True))
    if (
        cand.mapping_status == "auto_approved"
        and allowed
        and row.mapping_status != "manually_approved"
    ):
        row.mapping_status = "auto_approved"
        row.required_review = False
        row.active = True
    _log_usage(db, row.provider_code, now)
    db.flush()
    return row


def _log_usage(db: Session, provider_code: str, now: datetime) -> None:
    db.add(
        ProviderUsage(
            provider=provider_code,
            operation="enrichment",
            request_count=1,
            product_count=1,
            started_at=now,
            completed_at=now,
        )
    )
    db.flush()


def enrichment_budget_state(
    db: Session, provider_code: str, settings: Settings | None = None
) -> dict[str, int]:
    settings = settings or get_settings()
    state = _budget(db, provider_code, settings, datetime.now(UTC))
    return {
        "used": state.used,
        "budget": state.budget,
        "remaining": max(0, state.budget - state.used),
    }


__all__ = [
    "BudgetState",
    "DetailFetcher",
    "EnrichmentFailed",
    "EnrichmentUnavailable",
    "enrich",
    "enrichment_budget_state",
]
