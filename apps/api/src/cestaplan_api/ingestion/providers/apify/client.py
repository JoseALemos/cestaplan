"""Apify HTTP transport (FASE 4, schema-independent).

Runs an Apify actor asynchronously and reads its dataset, owning only transport: Bearer auth
(never a query-string token), the start→poll→dataset flow, terminal-state handling and a wall
budget. It knows nothing about a specific actor's input/output schema (that lives in
``apify/mapping.py`` once a real smoke run produces fixtures).

Injectable ``httpx.Client``, ``sleep`` and ``monotonic`` make the whole flow testable offline.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from cestaplan_api.ingestion.providers.exceptions import (
    ProviderAuthError,
    ProviderResponseError,
)

# Apify run lifecycle. We normalize to lower/underscore for the domain.
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
_SUCCESS = "SUCCEEDED"


class ApifyRunError(ProviderResponseError):
    """An actor run reached a terminal state other than SUCCEEDED, or the wall budget elapsed."""


class ApifyClient:
    """Minimal async-run client for one Apify token/base URL."""

    def __init__(
        self,
        *,
        api_token: str,
        base_url: str = "https://api.apify.com/v2",
        timeout: float = 30.0,
        max_wait_seconds: int = 900,
        poll_interval_seconds: float = 10.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_token:
            raise ValueError("Apify api_token is required")
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._max_wait = max_wait_seconds
        self._poll = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def _headers(self) -> dict[str, str]:
        # Bearer token in the header, never as a ?token= query parameter.
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def start_run(self, actor_id: str, run_input: dict[str, Any]) -> str:
        """POST an actor run with a bounded input; return the run id."""
        url = f"{self._base_url}/acts/{actor_id}/runs"
        try:
            response = self._client.post(url, json=run_input, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"Apify start_run transport error: {type(exc).__name__}"
            ) from exc
        self._raise_for_auth(response)
        data = _data(_decode(response))
        run_id = data.get("id")
        if not isinstance(run_id, str):
            raise ProviderResponseError("Apify start_run returned no run id")
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        url = f"{self._base_url}/actor-runs/{run_id}"
        try:
            response = self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"Apify get_run transport error: {type(exc).__name__}"
            ) from exc
        self._raise_for_auth(response)
        return _data(_decode(response))

    def wait_for_run(self, run_id: str) -> dict[str, Any]:
        """Poll until the run reaches a terminal state; return the SUCCEEDED run.

        Raises :class:`ApifyRunError` on a non-success terminal state or when the wall budget
        (``max_wait_seconds``) elapses.
        """
        deadline = self._monotonic() + self._max_wait
        while True:
            run = self.get_run(run_id)
            status = str(run.get("status", "")).upper()
            if status in _TERMINAL:
                if status != _SUCCESS:
                    raise ApifyRunError(f"Apify run {run_id} ended {status}")
                return run
            if self._monotonic() >= deadline:
                raise ApifyRunError(f"Apify run {run_id} exceeded {self._max_wait}s budget")
            self._sleep(self._poll)

    def get_dataset_items(
        self, dataset_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}/datasets/{dataset_id}/items"
        params = {"clean": "true"}
        if limit is not None:
            params["limit"] = str(limit)
        try:
            response = self._client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"Apify dataset transport error: {type(exc).__name__}"
            ) from exc
        self._raise_for_auth(response)
        items = _decode(response)
        if not isinstance(items, list):
            raise ProviderResponseError("Apify dataset items response is not a list")
        return [item for item in items if isinstance(item, dict)]

    def _raise_for_auth(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise ProviderAuthError(f"Apify auth failed ({response.status_code})")

    def close(self) -> None:
        self._client.close()


def _decode(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise ProviderResponseError(f"Apify unexpected status {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise ProviderResponseError(f"Apify returned non-JSON body ({content_type!r})")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderResponseError("Apify returned an undecodable JSON body") from exc


def _data(payload: Any) -> dict[str, Any]:
    """Apify wraps most resources in ``{"data": {...}}``; tolerate both shapes."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload, dict):
        return payload
    raise ProviderResponseError("Apify response was not a JSON object")


__all__ = ["ApifyClient", "ApifyRunError"]
