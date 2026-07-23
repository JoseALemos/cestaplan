"""Provider transport clients (Parse.bot, Apify) + quota — fully offline.

Uses httpx.MockTransport and injected sleep/clock: no network, no real waiting. Verifies
auth handling, retry/backoff on 429/5xx, non-JSON rejection, the Apify async run flow and
its budget, and that the API key/token never leaks into the URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.orm import Session

from cestaplan_api.ingestion.providers.apify.client import ApifyClient, ApifyRunError
from cestaplan_api.ingestion.providers.exceptions import (
    ProviderAuthError,
    ProviderQuotaExceeded,
    ProviderRateLimited,
    ProviderResponseError,
)
from cestaplan_api.ingestion.providers.parsebot.client import ParseBotClient
from cestaplan_api.ingestion.providers.quota import check_quota, daily_usage, record_usage


def _parsebot(handler) -> tuple[ParseBotClient, list[float]]:
    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        ParseBotClient(
            base_url="https://api.parse.bot/scraper/abc",
            api_key="SECRET-KEY",
            max_retries=3,
            backoff_base=0.01,
            client=client,
            sleep=slept.append,
        ),
        slept,
    )


# --- Parse.bot ------------------------------------------------------------- #
def test_parsebot_success_sends_key_in_header_not_url() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-API-Key")
        return httpx.Response(200, json={"products": [1, 2]})

    client, _ = _parsebot(handler)
    assert client.get_json("/get_products_by_category", {"category": "leche"}) == {
        "products": [1, 2]
    }
    assert seen["key"] == "SECRET-KEY"
    assert "SECRET-KEY" not in seen["url"]  # key never in URL/query


def test_parsebot_401_raises_auth_without_leaking_key() -> None:
    client, _ = _parsebot(lambda req: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(ProviderAuthError) as exc:
        client.get_json("/x")
    assert "SECRET-KEY" not in str(exc.value)


def test_parsebot_retries_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"}, json={})
        return httpx.Response(200, json={"ok": True})

    client, slept = _parsebot(handler)
    assert client.get_json("/x") == {"ok": True}
    assert calls["n"] == 2 and slept  # backed off once


def test_parsebot_persistent_429_raises_rate_limited() -> None:
    client, _ = _parsebot(lambda req: httpx.Response(429, json={}))
    with pytest.raises(ProviderRateLimited):
        client.get_json("/x")


def test_parsebot_5xx_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"ok": 1}) if calls["n"] > 1 else httpx.Response(503)

    client, _ = _parsebot(handler)
    assert client.get_json("/x") == {"ok": 1}


def test_parsebot_non_json_body_raises() -> None:
    client, _ = _parsebot(lambda req: httpx.Response(200, text="<html>blocked</html>"))
    with pytest.raises(ProviderResponseError):
        client.get_json("/x")


# --- Apify ----------------------------------------------------------------- #
def _apify(handler, *, monotonic_values=None, max_wait=900) -> ApifyClient:
    ticks = iter(monotonic_values or [0.0] * 100)
    return ApifyClient(
        api_token="APIFY-TOKEN",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
        monotonic=lambda: next(ticks),
        max_wait_seconds=max_wait,
        poll_interval_seconds=0.01,
    )


def test_apify_run_flow_succeeds() -> None:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer APIFY-TOKEN"
        assert "APIFY-TOKEN" not in str(request.url)  # token never in URL
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run-1", "status": "RUNNING"}})
        if "/actor-runs/" in request.url.path:
            state["polls"] += 1
            status = "SUCCEEDED" if state["polls"] >= 2 else "RUNNING"
            return httpx.Response(
                200, json={"data": {"id": "run-1", "status": status, "defaultDatasetId": "ds-1"}}
            )
        return httpx.Response(200, json=[{"name": "Leche", "price": "0.88"}])

    client = _apify(handler)
    run_id = client.start_run("actor~x", {"maxItems": 10})
    assert run_id == "run-1"
    run = client.wait_for_run(run_id)
    assert run["defaultDatasetId"] == "ds-1"
    items = client.get_dataset_items("ds-1", limit=10)
    assert items == [{"name": "Leche", "price": "0.88"}]


def test_apify_failed_run_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "r", "status": "FAILED"}})

    with pytest.raises(ApifyRunError):
        _apify(handler).wait_for_run("r")


def test_apify_budget_timeout_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"id": "r", "status": "RUNNING"}})

    # deadline = monotonic()+0 = 0; next monotonic() = 1 >= 0 -> timeout.
    client = _apify(handler, monotonic_values=[0.0, 1.0, 2.0], max_wait=0)
    with pytest.raises(ApifyRunError):
        client.wait_for_run("r")


def test_apify_401_raises_auth() -> None:
    with pytest.raises(ProviderAuthError):
        _apify(lambda req: httpx.Response(401)).get_run("r")


# --- Quota ----------------------------------------------------------------- #
def test_quota_counts_and_enforces(db_session: Session) -> None:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    record_usage(
        db_session,
        "apify-mercadona",
        "run",
        started_at=now,
        estimated_cost=Decimal("3.00"),
        product_count=10,
    )
    record_usage(
        db_session,
        "apify-mercadona",
        "run",
        started_at=now,
        estimated_cost=Decimal("4.00"),
        product_count=8,
    )
    runs, cost = daily_usage(db_session, "apify-mercadona", now)
    assert runs == 2 and cost == Decimal("7.00")

    # under caps -> ok
    check_quota(db_session, "apify-mercadona", now, max_daily_runs=5, max_daily_cost_eur=10)
    # over run cap
    with pytest.raises(ProviderQuotaExceeded):
        check_quota(db_session, "apify-mercadona", now, max_daily_runs=2)
    # over cost cap
    with pytest.raises(ProviderQuotaExceeded):
        check_quota(db_session, "apify-mercadona", now, max_daily_cost_eur=7)
