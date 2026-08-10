"""Mercadona direct public-API transport (FREE; replaces the paid Apify actor).

Talks to Mercadona's OWN public store API (``tienda.mercadona.es/api``) over a cookie-backed
:class:`httpx.Client`. It fixes the delivery zone by postal code (``change-pc`` seals a session
cookie), walks the category tree (``/categories/``) and collects the products of every
(sub)category (``/categories/{id}/``). It owns only transport + politeness — never any
product-schema knowledge: the collected records have the SAME shape the Apify actor returned, so
they are mapped by the reused :class:`ApifyMercadonaMapper` and its schema fingerprint is unchanged.

Legal / courtesy footing (see ``providers/rights.py``):

- an identifiable ``User-Agent`` (and optional operator ``From`` contact header),
- a randomised inter-request delay between every category fetch (this is a ~200-request crawl —
  it is deliberately slow and polite),
- a MONTHLY refresh cadence (configured in the scheduler, not here),
- bounded timeouts/retries and strict respect for the site's ToS/robots.

Blocks / CAPTCHA are NEVER evaded. The injectable ``httpx.Client`` + ``sleep`` make the whole crawl
testable fully offline with :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from cestaplan_api.ingestion.providers.exceptions import ProviderResponseError

_CHANGE_PC_PATH = "postal-codes/actions/change-pc/"
_CATEGORIES_PATH = "categories/"


class MercadonaClient:
    """Cookie-backed crawler for one Mercadona delivery zone (postal code)."""

    def __init__(
        self,
        *,
        postal_code: str,
        user_agent: str,
        base_url: str = "https://tienda.mercadona.es/api",
        contact_email: str = "",
        timeout: float = 20.0,
        max_retries: int = 3,
        delay_bounds_seconds: tuple[float, float] = (0.5, 1.5),
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        delay: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not user_agent:
            raise ValueError("Mercadona user_agent is required")
        self._base_url = base_url.rstrip("/")
        self._postal_code = postal_code
        self._max_retries = max_retries
        lo, hi = delay_bounds_seconds
        self._delay_lo = max(0.0, lo)
        self._delay_hi = max(self._delay_lo, hi)
        self._sleep = sleep
        self._delay = delay
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if contact_email:
            self._headers["From"] = contact_email  # honest operator/abuse contact
        # A cookie jar is essential: change-pc pins the zone in a session cookie that must ride
        # every subsequent GET, so the catalogue we read is the configured zone's.
        self._client = client or httpx.Client(timeout=timeout, cookies=httpx.Cookies())

    @property
    def postal_code(self) -> str:
        return self._postal_code

    def set_postal_code(self) -> None:
        """Pin the delivery zone: POST ``change-pc`` so the session cookie zones later GETs.

        A no-op when no postal code is configured (the crawl then reflects the API's default
        warehouse). The response body is ignored — only the cookie the client stores matters.
        """
        if not self._postal_code:
            return
        url = f"{self._base_url}/{_CHANGE_PC_PATH}"
        try:
            response = self._client.post(
                url, json={"new_postal_code": self._postal_code}, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise ProviderResponseError(
                f"Mercadona change-pc transport error: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise ProviderResponseError(
                f"Mercadona change-pc unexpected status {response.status_code}"
            )

    def crawl_products(self, *, max_products: int | None = None) -> list[dict[str, Any]]:
        """Full-catalogue crawl: pin the zone, then collect every (sub)category's products.

        Products are de-duplicated by ``id`` across the whole crawl (a product can appear under
        more than one category). ``max_products`` bounds the collection (a discovery/test cap);
        ``None`` collects the whole zone catalogue.
        """
        self.set_postal_code()
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for category_id in self._leaf_category_ids():
            if max_products is not None and len(records) >= max_products:
                break
            detail = self._get_json(f"{_CATEGORIES_PATH}{category_id}/", courtesy=True)
            for product in _iter_products(detail):
                pid = product.get("id")
                if pid is None:
                    continue
                key = str(pid)
                if key in seen:
                    continue
                seen.add(key)
                records.append(product)
                if max_products is not None and len(records) >= max_products:
                    break
        return records

    def _leaf_category_ids(self) -> list[int]:
        """The leaf (deepest) category ids from ``/categories/`` — the nodes that carry products.

        The tree nests root categories -> subcategories; only the leaves are fetched (a parent's
        products live under its children). Order is preserved and ids are de-duplicated.
        """
        root = self._get_json(_CATEGORIES_PATH, courtesy=False)  # first read; change-pc was polite
        results = root.get("results") if isinstance(root, dict) else None
        ids: list[int] = []
        seen: set[int] = set()

        def walk(node: object) -> None:
            if not isinstance(node, dict):
                return
            children = node.get("categories")
            if isinstance(children, list) and children:
                for child in children:
                    walk(child)
                return
            cid = node.get("id")
            if isinstance(cid, int) and cid not in seen:
                seen.add(cid)
                ids.append(cid)

        if isinstance(results, list):
            for node in results:
                walk(node)
        return ids

    def _get_json(self, path: str, *, courtesy: bool) -> Any:
        """GET ``path`` and return decoded JSON, retrying transient failures.

        A randomised courtesy delay is slept BEFORE every crawl request (``courtesy=True``) so the
        ~200-request category sweep is politely paced. Transport errors and 5xx are retried with a
        fixed backoff; a 4xx is surfaced (never silently swallowed); a non-JSON body is refused.
        """
        if courtesy:
            self._sleep(self._delay(self._delay_lo, self._delay_hi))
        url = f"{self._base_url}/{path.lstrip('/')}"
        last_error = "unknown transport error"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(url, headers=self._headers)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {type(exc).__name__}"
                self._backoff(attempt)
                continue
            if response.status_code >= 500:
                last_error = f"server error ({response.status_code})"
                self._backoff(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderResponseError(f"Mercadona unexpected status {response.status_code}")
            return _decode_json(response)
        raise ProviderResponseError(f"Mercadona request failed after retries: {last_error}")

    def _backoff(self, attempt: int) -> None:
        if attempt < self._max_retries:
            self._sleep(self._delay_hi)

    def close(self) -> None:
        self._client.close()


def _iter_products(detail: object) -> Iterator[dict[str, Any]]:
    """Yield every product dict in a ``/categories/{id}/`` detail.

    The detail carries products both at the top level (``.products``) and nested inside grouped
    sub-sections (``.categories[].products``, recursively); both are collected.
    """

    def walk(node: object) -> Iterator[dict[str, Any]]:
        if not isinstance(node, dict):
            return
        products = node.get("products")
        if isinstance(products, list):
            for product in products:
                if isinstance(product, dict):
                    yield product
        children = node.get("categories")
        if isinstance(children, list):
            for child in children:
                yield from walk(child)

    yield from walk(detail)


def _decode_json(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        raise ProviderResponseError(
            f"Mercadona returned non-JSON body (content-type {content_type!r})"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderResponseError("Mercadona returned an undecodable JSON body") from exc


__all__ = ["MercadonaClient"]
