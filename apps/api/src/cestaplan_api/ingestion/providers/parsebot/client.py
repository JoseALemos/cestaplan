"""Parse.bot HTTP transport (FASE 2, schema-independent).

Thin authenticated JSON client for the Parse.bot REST API. It owns only transport concerns —
auth header, timeouts, retries with backoff, 429 handling, and turning transport failures
into typed provider exceptions — never any response-schema knowledge (that lives in each
scraper's ``mapping.py`` once real fixtures exist).

Security: the API key travels in the ``X-API-Key`` header, never in the URL or query, and is
never included in exception messages or logs. The client is constructed with an injectable
``httpx.Client`` and ``sleep`` so tests run fully offline with ``httpx.MockTransport``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from cestaplan_api.ingestion.providers.exceptions import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderResponseError,
)

_API_KEY_HEADER = "X-API-Key"


class ParseBotClient:
    """Authenticated JSON GET client for one Parse.bot scraper base URL."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("Parse.bot base_url is required")
        if not api_key:
            raise ValueError("Parse.bot api_key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=timeout)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return decoded JSON, retrying transient failures.

        Raises :class:`ProviderAuthError` on 401/403, :class:`ProviderRateLimited` on a
        persistent 429, and :class:`ProviderResponseError` on 5xx-after-retries or a
        non-JSON/HTML body. The API key never appears in any raised message.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        headers = {_API_KEY_HEADER: self._api_key, "Accept": "application/json"}
        last_error = "unknown transport error"

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                # Never interpolate the key; httpx errors carry only the (key-free) URL.
                last_error = f"transport error: {type(exc).__name__}"
                self._backoff(attempt)
                continue

            if response.status_code in (401, 403):
                raise ProviderAuthError(f"Parse.bot auth failed ({response.status_code})")
            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                if attempt < self._max_retries:
                    self._sleep(retry_after if retry_after is not None else self._delay(attempt))
                    last_error = "rate limited (429)"
                    continue
                raise ProviderRateLimited("Parse.bot rate limited (429)", retry_after)
            if response.status_code >= 500:
                last_error = f"server error ({response.status_code})"
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderResponseError(f"Parse.bot unexpected status {response.status_code}")
            return _decode_json(response)

        raise ProviderResponseError(f"Parse.bot request failed after retries: {last_error}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            self._sleep(self._delay(attempt))

    def _delay(self, attempt: int) -> float:
        return self._backoff_base * (2**attempt)

    def close(self) -> None:
        self._client.close()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _decode_json(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise ProviderResponseError(
            f"Parse.bot returned non-JSON body (content-type {content_type!r})"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderResponseError("Parse.bot returned an undecodable JSON body") from exc


__all__ = ["ParseBotClient"]
