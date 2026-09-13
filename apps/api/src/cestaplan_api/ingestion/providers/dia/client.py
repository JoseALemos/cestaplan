"""DIA direct public-API transport (FREE; no paid feed, no API key).

Talks to DIA's OWN public search API (``www.dia.es/api/v1/search-back/search``) over an injectable
:class:`httpx.Client`. It owns only transport + politeness — never any product-schema knowledge:
the collected ``search_items`` records are mapped by :class:`DiaMapper` in ``dia/mapping.py``.

The search endpoint is a paginated GET (``?q=<term>&page=<n>``); every page returns up to
``page_size`` (30) items plus a ``pagination`` block with ``total_pages``. This client walks
``page=1..total_pages`` and returns every collected ``search_item``.

Legal / courtesy footing (see ``providers/rights.py``):

- an identifiable ``User-Agent`` (and optional operator ``From`` contact header),
- a randomised inter-request delay between every page fetch (deliberately polite pacing),
- a MONTHLY refresh cadence (configured in the scheduler, not here),
- bounded timeouts/retries; a 403/429 is treated as a block signal — the client BACKS OFF and
  stops, it never retries aggressively and never evades a block/CAPTCHA.

TODO(zone-pinning): the ``?postal_code=`` query param is ignored by DIA; the API serves its
DEFAULT national warehouse (``cart.postal_code`` 28041). Store-scope zone-pinning (e.g. Córdoba
14006) is a follow-up that needs a session/cookie ``set-postal`` call first; v1 uses the national
default zone only.

The injectable ``httpx.Client`` + ``sleep``/``delay`` make the whole flow testable fully offline
with :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from cestaplan_api.ingestion.providers.exceptions import ProviderResponseError

_SEARCH_PATH = "api/v1/search-back/search"
# Statuses that mean "you are being blocked / rate limited": back off and stop, never hammer.
_BLOCK_STATUSES = (403, 429)


class DiaClient:
    """Polite, paginated crawler for DIA's public search API (national default zone)."""

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = "https://www.dia.es",
        contact_email: str = "",
        timeout: float = 20.0,
        max_retries: int = 3,
        delay_bounds_seconds: tuple[float, float] = (0.5, 1.5),
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        delay: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not user_agent:
            raise ValueError("DIA user_agent is required")
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        lo, hi = delay_bounds_seconds
        self._delay_lo = max(0.0, lo)
        self._delay_hi = max(self._delay_lo, hi)
        self._sleep = sleep
        self._delay = delay
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if contact_email:
            self._headers["From"] = contact_email  # honest operator/abuse contact
        self._client = client or httpx.Client(timeout=timeout)

    def search(self, term: str, *, max_products: int | None = None) -> list[dict[str, Any]]:
        """Search ``term`` and collect every ``search_item`` across pages (respecting total_pages).

        ``max_products`` bounds the collection (a discovery/test cap); ``None`` collects every page.
        A 403/429 block signal stops the walk cleanly (surfaced as :class:`ProviderResponseError`).
        """
        records: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            if max_products is not None and len(records) >= max_products:
                break
            payload = self._get_json(_SEARCH_PATH, params={"q": term, "page": page})
            for item in _iter_items(payload):
                records.append(item)
                if max_products is not None and len(records) >= max_products:
                    break
            total_pages = _total_pages(payload, current=page)
            page += 1
        return records

    def _get_json(self, path: str, *, params: dict[str, Any]) -> Any:
        """GET ``path`` with a courtesy delay, retrying only transient (transport/5xx) failures.

        A 403/429 is a block signal: it is surfaced immediately and NEVER retried (courtesy — the
        connector backs off rather than hammering a site that is asking it to stop). Other 4xx are
        surfaced; a non-JSON body is refused. 5xx and transport errors get a bounded backoff retry.
        """
        self._sleep(self._delay(self._delay_lo, self._delay_hi))  # polite pacing before every GET
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_error = "unknown transport error"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {type(exc).__name__}"
                self._backoff(attempt)
                continue
            if response.status_code in _BLOCK_STATUSES:
                # Block/rate-limit: stop, do not retry, do not evade.
                raise ProviderResponseError(
                    f"DIA responded {response.status_code} (blocked/rate-limited); backing off"
                )
            if response.status_code >= 500:
                last_error = f"server error ({response.status_code})"
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderResponseError(f"DIA unexpected status {response.status_code}")
            return _decode_json(response)
        raise ProviderResponseError(f"DIA request failed after retries: {last_error}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            self._sleep(self._delay_hi)

    def close(self) -> None:
        self._client.close()


def _iter_items(payload: object) -> list[dict[str, Any]]:
    """The ``search_items`` list of one search page (empty when absent/ill-typed)."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("search_items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _total_pages(payload: object, *, current: int) -> int:
    """``pagination.total_pages`` from a search page; falls back to the current page when absent."""
    if isinstance(payload, dict):
        pagination = payload.get("pagination")
        if isinstance(pagination, dict):
            total = pagination.get("total_pages")
            if isinstance(total, int) and total > 0:
                return total
    return current


def _decode_json(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise ProviderResponseError(f"DIA returned non-JSON body (content-type {content_type!r})")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderResponseError("DIA returned an undecodable JSON body") from exc


__all__ = ["DiaClient"]
