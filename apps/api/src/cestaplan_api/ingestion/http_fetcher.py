"""Resilient, polite HTTP layer for the price-ingestion subsystem (FASE A, spec §6).

This is the single network chokepoint every retailer connector fetches through. It is
deliberately conservative and defensive: it only ever talks to hosts a connector declares
in its :class:`~cestaplan_api.ingestion.SourcePolicy`, honours per-domain rate limits, and
**never** attempts to solve or evade a block page / CAPTCHA / login wall — it detects and
reports them so the caller can stop.

Guarantees:

- **Politeness.** Bounded timeout, limited retries with exponential backoff + jitter,
  per-domain max concurrency and a configurable (jittered) delay between requests to the
  same domain, an honest and identifiable ``User-Agent`` (+ optional ``From`` contact).
- **Change detection.** Conditional GET (``If-None-Match`` / ``If-Modified-Since`` from a
  prior capture) with 304 handled as *unchanged*; a sha256 content hash detects no-change
  even when the source omits validators.
- **Circuit breaker.** After ``connector_failure_threshold`` consecutive failures for a
  domain the circuit opens for ``connector_circuit_open_minutes`` and further fetches
  short-circuit instead of hammering the source.
- **Safety.** Response-size cap (aborts oversized downloads), MIME validation, a domain
  allowlist and an SSRF guard (reject private/loopback/link-local IPs and non-http(s)
  schemes; cross-host redirects are returned, never auto-followed). Cancellation support.
- **No secrets leak.** Stored/returned headers have ``Authorization`` / ``Cookie`` /
  ``Set-Cookie`` and any token-like header **redacted**.

Nothing here decides *what* to fetch or *whether* a source is allowed to be scraped — that
is the connector's :class:`SourcePolicy` and the operator's opt-in configuration.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

from cestaplan_api.config import Settings, get_settings
from cestaplan_api.ingestion.contracts import SourcePolicy

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Header names whose value must never be stored/returned (case-insensitive, exact).
REDACT_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)

#: Substrings that mark a header name as sensitive (redacted defensively).
_REDACT_HEADER_SUBSTRINGS: tuple[str, ...] = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "api-key",
)

#: The placeholder written in place of a redacted header value.
REDACTED = "REDACTED"

#: HTTP statuses worth a retry (transient server-side / rate-limit-free failures).
RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})

#: Statuses that most likely indicate an anti-bot / rate-limit block page.
_BLOCK_STATUS: frozenset[int] = frozenset({403, 429})

#: Lowercased challenge markers used to flag a block/CAPTCHA/login interstitial.
_BLOCK_MARKERS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "px-captcha",
    "cf-chl",
    "cf-challenge",
    "just a moment",
    "attention required",
    "access denied",
    "are you a robot",
    "are you human",
    "verify you are human",
    "unusual traffic",
    "bot detection",
    "please enable javascript and cookies",
    "distil",
    "incapsula",
)

#: A response smaller than this that also mentions a login/captcha word is suspect.
_TINY_BODY_BYTES = 2048
_LOGIN_MARKERS: tuple[str, ...] = ("captcha", "log in", "login", "sign in", "iniciar sesión")


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PriorCapture:
    """Conditional-GET validators carried over from a previously stored capture."""

    etag: str | None = None
    last_modified: str | None = None
    body_hash: str | None = None


@dataclass(frozen=True, slots=True)
class HttpFetchResult:
    """Outcome of a single HTTP fetch through :class:`HttpFetcher`.

    ``headers`` are always redacted. ``content`` is the raw body bytes (``None`` for a 304 or
    an aborted/errored fetch). ``body_hash`` is the sha256 of the body (or the prior body's
    hash on an unchanged response). ``not_modified`` marks a 304 or a content-hash match;
    ``from_cache`` that the prior capture's body is still current. ``is_block_page`` flags an
    anti-bot interstitial (never solved). ``circuit_open`` marks a short-circuited fetch.
    """

    url: str
    ok: bool = False
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes | None = None
    content_type: str | None = None
    body_hash: str | None = None
    from_cache: bool = False
    not_modified: bool = False
    is_block_page: bool = False
    circuit_open: bool = False
    redirect_location: str | None = None
    elapsed_ms: int = 0
    error: str | None = None


# --------------------------------------------------------------------------- #
# Header redaction & block-page detection (pure helpers, reused by capture.py)
# --------------------------------------------------------------------------- #


def _is_sensitive_header(name: str) -> bool:
    lowered = name.lower()
    if lowered in REDACT_HEADER_NAMES:
        return True
    return any(sub in lowered for sub in _REDACT_HEADER_SUBSTRINGS)


def redact_headers(headers: object) -> dict[str, str]:
    """Return a plain dict of headers with every sensitive value replaced by ``REDACTED``.

    Accepts anything dict-like / iterable of pairs (e.g. :class:`httpx.Headers`). Never
    raises; unknown shapes yield ``{}``.
    """
    items: list[tuple[str, str]] = []
    if hasattr(headers, "multi_items"):
        items = [(str(k), str(v)) for k, v in headers.multi_items()]  # type: ignore[attr-defined]
    elif isinstance(headers, dict):
        items = [(str(k), str(v)) for k, v in headers.items()]
    elif headers is not None:
        try:
            items = [(str(k), str(v)) for k, v in headers]  # type: ignore[misc]
        except (TypeError, ValueError):
            items = []
    out: dict[str, str] = {}
    for name, value in items:
        out[name] = REDACTED if _is_sensitive_header(name) else value
    return out


def detect_block_page(
    status_code: int | None, headers: object, body: bytes | None
) -> bool:
    """Heuristically flag a response as an anti-bot / CAPTCHA / login interstitial.

    Never attempts to solve or bypass anything — it only reports so the caller can stop.
    Signals: a 403/429 status, a known challenge marker in the body, or a tiny body that
    mentions a login/captcha word.
    """
    if status_code in _BLOCK_STATUS:
        return True
    if not body:
        return False
    # Decode defensively; block pages are HTML/text.
    text = body[:65536].decode("utf-8", errors="ignore").lower()
    if any(marker in text for marker in _BLOCK_MARKERS):
        return True
    if len(body) <= _TINY_BODY_BYTES and any(word in text for word in _LOGIN_MARKERS):
        return True
    # A well-known anti-bot cookie/header is a strong signal even on a 200.
    reduced = redact_headers(headers)
    server = (reduced.get("server") or "").lower()
    return "cloudflare" in server and status_code == 503


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #


def _default_resolver(host: str) -> list[str]:
    """Resolve a hostname to its IP strings via the OS resolver (empty list on failure)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    return [str(info[4][0]) for info in infos]


def _ip_is_forbidden(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable -> treat as unsafe
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


# --------------------------------------------------------------------------- #
# The fetcher
# --------------------------------------------------------------------------- #


class HttpFetcher:
    """A resilient, polite, injectable HTTP client for connectors.

    Sync (matching the codebase's ``httpx.Client`` adapters). A client may be injected —
    e.g. an ``httpx.Client(transport=httpx.MockTransport(...))`` in tests — and is reused,
    not closed here. ``sleep``/``monotonic``/``now``/``rng``/``resolver`` are injectable so
    backoff, per-domain delay, the circuit clock and DNS are fully deterministic under test.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        settings: Settings | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
        rng: Callable[[float, float], float] | None = None,
        resolver: Callable[[str], list[str]] = _default_resolver,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._now = now or (lambda: datetime.now(UTC))
        # rng(lo, hi) -> a value in [lo, hi]; default uniform jitter.
        import random as _random

        self._rng = rng or _random.uniform
        self._resolver = resolver

        self._lock = threading.Lock()
        self._domain_semaphores: dict[str, threading.Semaphore] = {}
        self._domain_last_request: dict[str, float] = {}
        # domain -> (consecutive_failures, circuit_open_until_monotonic | None)
        self._breaker: dict[str, tuple[int, float | None]] = {}

    # -- public API ------------------------------------------------------ #
    def fetch(
        self,
        url: str,
        *,
        policy: SourcePolicy,
        method: str = "GET",
        prior: PriorCapture | None = None,
        extra_headers: dict[str, str] | None = None,
        allowed_content_types: tuple[str, ...] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> HttpFetchResult:
        """Fetch ``url`` honouring ``policy``; returns a controlled :class:`HttpFetchResult`.

        Never raises for a network/source problem — every failure mode is reported in the
        result (``error``/``is_block_page``/``circuit_open``). ``prior`` enables conditional
        GET + content-hash change detection. ``allowed_content_types`` (base MIME strings)
        enables MIME validation. ``cancel`` is polled to abort early.
        """
        start = self._monotonic()

        if cancel is not None and cancel():
            return self._result(url, start, error="cancelled")

        guard = self._preflight(url, policy)
        if guard is not None:
            return replace(guard, elapsed_ms=self._elapsed_ms(start))

        domain = urlsplit(url).hostname or ""

        if self._circuit_is_open(domain):
            return self._result(
                url, start, circuit_open=True, error="circuit_open"
            )

        semaphore = self._semaphore_for(domain, policy)
        semaphore.acquire()
        try:
            self._respect_delay(domain, policy)
            if cancel is not None and cancel():
                return self._result(url, start, error="cancelled")
            result = self._attempt_with_retries(
                url,
                start=start,
                method=method,
                prior=prior,
                extra_headers=extra_headers,
                allowed_content_types=allowed_content_types,
                cancel=cancel,
            )
        finally:
            self._domain_last_request[domain] = self._monotonic()
            semaphore.release()

        self._record_outcome(domain, result)
        return result

    # -- pre-flight guards ----------------------------------------------- #
    def _preflight(self, url: str, policy: SourcePolicy) -> HttpFetchResult | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return self._result(url, self._monotonic(), error="scheme_not_allowed")
        host = parts.hostname
        if not host:
            return self._result(url, self._monotonic(), error="invalid_url")

        allowed = tuple(d.lower() for d in policy.allowed_domains)
        if allowed and not _host_in_allowlist(host, allowed):
            return self._result(url, self._monotonic(), error="domain_not_allowed")

        # SSRF: reject IP literals / resolved IPs that are private/loopback/link-local.
        ip_candidates = self._resolve_for_ssrf(host)
        if ip_candidates is None:
            return self._result(url, self._monotonic(), error="dns_resolution_failed")
        if any(_ip_is_forbidden(ip) for ip in ip_candidates):
            return self._result(url, self._monotonic(), error="ssrf_blocked")
        return None

    def _resolve_for_ssrf(self, host: str) -> list[str] | None:
        """Return the IPs to SSRF-check for ``host`` (``None`` = resolution failed)."""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            resolved = self._resolver(host)
            return resolved or None
        return [host]

    # -- retry loop ------------------------------------------------------ #
    def _attempt_with_retries(
        self,
        url: str,
        *,
        start: float,
        method: str,
        prior: PriorCapture | None,
        extra_headers: dict[str, str] | None,
        allowed_content_types: tuple[str, ...] | None,
        cancel: Callable[[], bool] | None,
    ) -> HttpFetchResult:
        max_retries = max(0, self._settings.scraping_max_retries)
        attempt = 0
        last: HttpFetchResult | None = None
        while True:
            if cancel is not None and cancel():
                return self._result(url, start, error="cancelled")
            try:
                last = self._single_attempt(
                    url,
                    start=start,
                    method=method,
                    prior=prior,
                    extra_headers=extra_headers,
                    allowed_content_types=allowed_content_types,
                    cancel=cancel,
                )
                retryable = last.status_code in RETRYABLE_STATUS and not last.is_block_page
            except httpx.TimeoutException:
                last = self._result(url, start, error="timeout")
                retryable = True
            except httpx.HTTPError as exc:
                last = self._result(url, start, error=f"transport_error: {type(exc).__name__}")
                retryable = True

            if last.ok or last.not_modified or not retryable or attempt >= max_retries:
                return last
            attempt += 1
            self._sleep(self._backoff_seconds(attempt))

    def _single_attempt(
        self,
        url: str,
        *,
        start: float,
        method: str,
        prior: PriorCapture | None,
        extra_headers: dict[str, str] | None,
        allowed_content_types: tuple[str, ...] | None,
        cancel: Callable[[], bool] | None,
    ) -> HttpFetchResult:
        request_headers = self._build_headers(prior, extra_headers)
        max_bytes = self._settings.scraping_max_response_bytes
        timeout = float(self._settings.scraping_timeout_seconds)

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.Client(timeout=timeout, follow_redirects=False)
        try:
            with client.stream(
                method, url, headers=request_headers, timeout=timeout
            ) as response:
                status = response.status_code
                redacted = redact_headers(response.headers)
                content_type = response.headers.get("content-type")

                # 304: unchanged — carry the prior body hash so a capture still has one.
                # (Checked before the 3xx branch since 304 is numerically a 3xx.)
                if status == 304:
                    return self._result(
                        url,
                        start,
                        status_code=status,
                        headers=redacted,
                        content_type=content_type,
                        body_hash=prior.body_hash if prior else None,
                        not_modified=True,
                        from_cache=True,
                    )

                # 3xx: return the redirect for the caller to decide (never auto-follow).
                if 300 <= status < 400:
                    return self._result(
                        url,
                        start,
                        status_code=status,
                        headers=redacted,
                        content_type=content_type,
                        redirect_location=response.headers.get("location"),
                    )

                # Size guard via Content-Length before reading anything.
                declared = _content_length(response.headers)
                if declared is not None and declared > max_bytes:
                    return self._result(
                        url,
                        start,
                        status_code=status,
                        headers=redacted,
                        content_type=content_type,
                        error="response_too_large",
                    )

                # MIME validation (base type only).
                if allowed_content_types is not None and not _mime_allowed(
                    content_type, allowed_content_types
                ):
                    return self._result(
                        url,
                        start,
                        status_code=status,
                        headers=redacted,
                        content_type=content_type,
                        error="mime_not_allowed",
                    )

                # Stream the body, aborting if it exceeds the cap.
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if cancel is not None and cancel():
                        return self._result(
                            url,
                            start,
                            status_code=status,
                            headers=redacted,
                            content_type=content_type,
                            error="cancelled",
                        )
                    total += len(chunk)
                    if total > max_bytes:
                        return self._result(
                            url,
                            start,
                            status_code=status,
                            headers=redacted,
                            content_type=content_type,
                            error="response_too_large",
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
        finally:
            if owns_client:
                client.close()

        body_hash = hashlib.sha256(body).hexdigest()
        is_block = detect_block_page(status, response.headers, body)
        not_modified = prior is not None and prior.body_hash == body_hash
        ok = 200 <= status < 300 and not is_block

        return self._result(
            url,
            start,
            status_code=status,
            headers=redacted,
            content=body,
            content_type=content_type,
            body_hash=body_hash,
            not_modified=not_modified,
            from_cache=not_modified,
            is_block_page=is_block,
            ok=ok,
            error=None if ok else ("block_page" if is_block else f"http_{status}"),
        )

    # -- headers --------------------------------------------------------- #
    def _build_headers(
        self, prior: PriorCapture | None, extra_headers: dict[str, str] | None
    ) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": self._settings.scraping_user_agent}
        contact = self._settings.scraping_contact_email.strip()
        if contact:
            headers["From"] = contact
        if prior is not None:
            if prior.etag:
                headers["If-None-Match"] = prior.etag
            if prior.last_modified:
                headers["If-Modified-Since"] = prior.last_modified
        if extra_headers:
            headers.update(extra_headers)
        return headers

    # -- per-domain concurrency & delay ---------------------------------- #
    def _semaphore_for(self, domain: str, policy: SourcePolicy) -> threading.Semaphore:
        limit = max(1, min(policy.max_concurrency, self._settings.scraping_max_concurrency))
        with self._lock:
            sem = self._domain_semaphores.get(domain)
            if sem is None:
                sem = threading.Semaphore(limit)
                self._domain_semaphores[domain] = sem
            return sem

    def _respect_delay(self, domain: str, policy: SourcePolicy) -> None:
        last = self._domain_last_request.get(domain)
        if last is None:
            return
        elapsed = self._monotonic() - last
        wait = self._delay_seconds(policy) - elapsed
        if wait > 0:
            self._sleep(wait)

    def _delay_seconds(self, policy: SourcePolicy) -> float:
        """Jittered per-domain delay (seconds), clamped to at least the policy delay."""
        lo, hi = self._settings.scraping_request_delay_bounds_seconds
        lo = max(lo, policy.request_delay)
        hi = max(lo, hi)
        return self._rng(lo, hi)

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with additive jitter for retry ``attempt`` (1-based)."""
        base, _ = self._settings.scraping_request_delay_bounds_seconds
        base = base or 0.5
        window = base * (2 ** (attempt - 1))
        return window + self._rng(0.0, base)

    # -- circuit breaker ------------------------------------------------- #
    def _circuit_is_open(self, domain: str) -> bool:
        with self._lock:
            state = self._breaker.get(domain)
            if state is None:
                return False
            _failures, open_until = state
            if open_until is None:
                return False
            if self._monotonic() >= open_until:
                # Half-open: clear the timer, keep the failure count for observability.
                self._breaker[domain] = (0, None)
                return False
            return True

    def _record_outcome(self, domain: str, result: HttpFetchResult) -> None:
        succeeded = result.ok or result.not_modified or (
            result.status_code is not None and 300 <= result.status_code < 400
        )
        with self._lock:
            failures, _open_until = self._breaker.get(domain, (0, None))
            if succeeded:
                self._breaker[domain] = (0, None)
                return
            failures += 1
            open_until: float | None = None
            if failures >= max(1, self._settings.connector_failure_threshold):
                open_until = self._monotonic() + (
                    self._settings.connector_circuit_open_minutes * 60
                )
            self._breaker[domain] = (failures, open_until)

    # -- result construction --------------------------------------------- #
    def _result(
        self,
        url: str,
        start: float,
        **kwargs: object,
    ) -> HttpFetchResult:
        return HttpFetchResult(url=url, elapsed_ms=self._elapsed_ms(start), **kwargs)  # type: ignore[arg-type]

    def _elapsed_ms(self, start: float) -> int:
        return max(0, int((self._monotonic() - start) * 1000))


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def _host_in_allowlist(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower()
    return any(host == entry or host.endswith("." + entry) for entry in allowed)


def _content_length(headers: httpx.Headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _mime_allowed(content_type: str | None, allowed: tuple[str, ...]) -> bool:
    if content_type is None:
        return False
    base = content_type.split(";", 1)[0].strip().lower()
    return base in {a.lower() for a in allowed}


def expires_from_now(retention_days: int, *, now: datetime | None = None) -> datetime:
    """Compute a RawCapture ``expires_at`` from a retention horizon in days."""
    moment = now or datetime.now(UTC)
    return moment + timedelta(days=max(0, retention_days))
