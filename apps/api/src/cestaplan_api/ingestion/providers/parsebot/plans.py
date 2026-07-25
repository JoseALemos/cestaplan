"""Per-chain Parse.bot capture plans — the single source of truth for how each scraper's
sample is fetched (used by both the capture tool and the providers).

Each plan performs a bounded, deterministic flow against the chain's own scraper API and returns
a flat ``list[dict]`` of raw product/offer records (never more than ``limit``). The endpoint
names, params and list keys here were confirmed against real captures (see
``.local/provider-samples/<provider>/api-spec.json``); a plan is grounded only in the observed
contract — it never guesses an endpoint. No secrets are handled here (the client owns the key).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cestaplan_api.config import Settings
from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient


def _inner(data: Any) -> Any:
    """Unwrap the Parse.bot ``{"data": ..., "status": ...}`` envelope."""
    return data.get("data", data) if isinstance(data, dict) else data


def _cap_dia(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    d = _inner(client.get_json("/search_products", {"query": query, "limit": limit}))
    items = d.get("search_items", []) if isinstance(d, dict) else []
    return list(items)[:limit]


def _cap_alcampo(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    d = _inner(client.get_json("/search_products", {"query": query, "page_size": limit}))
    out: list[dict] = []
    for group in d.get("productGroups", []) if isinstance(d, dict) else []:
        if isinstance(group, dict):
            out.extend(p for p in group.get("decoratedProducts", []) if isinstance(p, dict))
    return out[:limit]


def _cap_carrefour(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    cats = _inner(client.get_json("/get_categories", {"postal_code": postal}))
    catlist = cats.get("categories", []) if isinstance(cats, dict) else []
    if not catlist:
        return []

    # Prefer a grocery-looking category; never navigate more than this one category (bounded).
    def _score(c: dict) -> int:
        name = (c.get("name") or "").lower()
        return (
            1
            if any(w in name for w in ("frescos", "aliment", "despensa", "bebida", "lácteo"))
            else 0
        )

    chosen = sorted(catlist, key=_score, reverse=True)[0]
    cid = chosen.get("category_id")
    if not cid:
        return []
    d = _inner(
        client.get_json(
            "/get_products_by_category",
            {"category_id": cid, "postal_code": postal, "limit": limit},
        )
    )
    prods = d.get("products", []) if isinstance(d, dict) else []
    return list(prods)[:limit]


def _cap_aldi(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    d = _inner(client.get_json("/get_current_offers", {"limit": limit}))
    offers = d.get("offers", []) if isinstance(d, dict) else []
    return list(offers)[:limit]


def _cap_lidl(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    # Lidl's product listing is store-scoped: resolve a store first. ``find_stores`` takes a
    # location term (not the product query), so a fixed city is used for the bounded capture.
    st = _inner(client.get_json("/find_stores", {"query": "Madrid"}))
    stores = st.get("stores", []) if isinstance(st, dict) else []
    if not stores:
        return []
    sid = stores[0].get("store_id")
    if not sid:
        return []
    d = _inner(client.get_json("/get_visible_products", {"store_id": sid, "limit": limit}))
    prods = d.get("products", []) if isinstance(d, dict) else []
    return list(prods)[:limit]


def _cap_deza(client: ParseBotClient, *, limit: int, query: str, postal: str) -> list[dict]:
    d = _inner(client.get_json("/get_current_offers", {"limit": limit}))
    offers = d.get("offers", []) if isinstance(d, dict) else []
    return list(offers)[:limit]


CaptureFn = Callable[..., list[dict]]

# provider_code -> (settings base-url attribute, capture function)
_PLANS: dict[str, tuple[str, CaptureFn]] = {
    "parsebot-dia": ("parse_bot_dia_base_url", _cap_dia),
    "parsebot-alcampo": ("parse_bot_alcampo_base_url", _cap_alcampo),
    "parsebot-carrefour": ("parse_bot_carrefour_base_url", _cap_carrefour),
    "parsebot-aldi": ("parse_bot_aldi_base_url", _cap_aldi),
    "parsebot-lidl": ("parse_bot_lidl_base_url", _cap_lidl),
    "parsebot-deza": ("parse_bot_deza_base_url", _cap_deza),
}


_ENABLED_ATTRS: dict[str, str] = {
    "parsebot-dia": "parse_bot_dia_enabled",
    "parsebot-alcampo": "parse_bot_alcampo_enabled",
    "parsebot-carrefour": "parse_bot_carrefour_enabled",
    "parsebot-aldi": "parse_bot_aldi_enabled",
    "parsebot-lidl": "parse_bot_lidl_enabled",
    "parsebot-deza": "parse_bot_deza_enabled",
}


def has_plan(provider_code: str) -> bool:
    return provider_code in _PLANS


def base_url_attr(provider_code: str) -> str:
    return _PLANS[provider_code][0]


def enabled_attr(provider_code: str) -> str:
    return _ENABLED_ATTRS[provider_code]


def is_configured(provider_code: str, settings: Settings) -> bool:
    """Whether a chain may reach the network: globally + per-chain enabled AND key + base URL set.

    A present base URL with the per-chain flag OFF never enables network access; a flag ON with no
    base URL (or no key) stays blocked. This is the single gate every network path consults.
    """
    if provider_code not in _PLANS:
        return False
    if not settings.parse_bot_enabled:
        return False
    if not getattr(settings, _ENABLED_ATTRS[provider_code], False):
        return False
    if not settings.parse_bot_api_key:
        return False
    return bool(getattr(settings, _PLANS[provider_code][0], "") or "")


def capture_records(
    provider_code: str,
    settings: Settings,
    *,
    limit: int,
    query: str = "leche",
    client: ParseBotClient | None = None,
) -> list[dict]:
    """Run the chain's bounded capture flow and return a flat list of raw records (<= limit)."""
    if provider_code not in _PLANS:
        raise ValueError(f"no capture plan for {provider_code!r}")
    attr, fn = _PLANS[provider_code]
    # Enable gate FIRST: a disabled chain never builds a client or touches the network, even with a
    # base URL present.
    chain_enabled = getattr(settings, _ENABLED_ATTRS[provider_code], False)
    if not settings.parse_bot_enabled or not chain_enabled:
        raise RuntimeError(f"{provider_code} está deshabilitado (flag enabled off)")
    base = getattr(settings, attr, "") or ""
    if not settings.parse_bot_api_key:
        raise RuntimeError("PARSE_BOT_API_KEY no está configurada")
    if not base:
        raise RuntimeError(f"base URL de {provider_code} no configurada")
    cli = client or ParseBotClient(
        base_url=base,
        api_key=settings.parse_bot_api_key,
        timeout=settings.parse_bot_timeout_seconds,
        max_retries=settings.parse_bot_max_retries,
    )
    postal = settings.price_provider_test_postal_code
    return fn(cli, limit=min(limit, 10), query=query, postal=postal)


__all__ = ["base_url_attr", "capture_records", "enabled_attr", "has_plan", "is_configured"]
