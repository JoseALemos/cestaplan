"""HttpFetcher tests — HTTPX fully mocked via ``httpx.MockTransport`` (NO network).

Covers the resilient-fetch guarantees of spec §6: backoff+retry, jittered delay bounds,
conditional GET (ETag / 304 / content-hash no-change), the per-domain circuit breaker,
the response-size abort, MIME validation, the SSRF guard + domain allowlist, block-page
detection (which NEVER tries to solve the challenge), header redaction and cancellation.

Every test injects a no-op ``sleep`` and a deterministic clock/rng so nothing waits and
nothing touches DNS or the network.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from cestaplan_api.config import Settings
from cestaplan_api.ingestion import SourcePolicy
from cestaplan_api.ingestion.http_fetcher import (
    HttpFetcher,
    PriorCapture,
    detect_block_page,
    redact_headers,
)

# A generous allowlist + a resolver that maps every test host to a PUBLIC IP so the SSRF
# guard passes for the happy paths (private/loopback hosts are tested explicitly below).
_POLICY = SourcePolicy(allowed_domains=("prices.example.com",), max_concurrency=2)
_URL = "https://prices.example.com/product/1"


def _public_resolver(host: str) -> list[str]:
    mapping = {
        "prices.example.com": ["93.184.216.34"],
        "big.example.com": ["93.184.216.34"],
        "blocked.example.com": ["93.184.216.34"],
    }
    return mapping.get(host, ["93.184.216.34"])


def _clock() -> Callable[[], float]:
    ticks = {"t": 0.0}

    def monotonic() -> float:
        ticks["t"] += 0.001
        return ticks["t"]

    return monotonic


def _make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: Settings | None = None,
    resolver: Callable[[str], list[str]] = _public_resolver,
    rng: Callable[[float, float], float] | None = None,
) -> HttpFetcher:
    transport = httpx.MockTransport(handler)
    return HttpFetcher(
        client=httpx.Client(transport=transport, follow_redirects=False),
        settings=settings or Settings(),
        sleep=lambda _seconds: None,
        monotonic=_clock(),
        rng=rng or (lambda lo, _hi: lo),
        resolver=resolver,
    )


# --------------------------------------------------------------------------- #
# Backoff + retry
# --------------------------------------------------------------------------- #


def test_retries_on_500_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text="ok-body")

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert calls["n"] == 2
    assert result.ok is True
    assert result.status_code == 200
    assert result.content == b"ok-body"


def test_retries_exhausted_returns_last_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="still down")

    settings = Settings(scraping_max_retries=2)
    result = _make_fetcher(handler, settings=settings).fetch(_URL, policy=_POLICY)
    assert result.ok is False
    assert result.status_code == 503
    assert result.error == "http_503"


def test_backoff_is_exponential_with_jitter_bounds() -> None:
    fetcher = _make_fetcher(lambda r: httpx.Response(200))
    base, _ = Settings().scraping_request_delay_bounds_seconds  # 0.5s
    for attempt in range(1, 4):
        low = base * (2 ** (attempt - 1))
        delay = fetcher._backoff_seconds(attempt)
        assert low <= delay <= low + base


def test_jittered_delay_within_configured_bounds() -> None:
    import random

    rng = random.Random(1234)
    fetcher = _make_fetcher(
        lambda r: httpx.Response(200), rng=lambda lo, hi: rng.uniform(lo, hi)
    )
    lo, hi = Settings().scraping_request_delay_bounds_seconds
    for _ in range(50):
        delay = fetcher._delay_seconds(_POLICY)
        assert lo <= delay <= hi


# --------------------------------------------------------------------------- #
# Conditional GET / change detection
# --------------------------------------------------------------------------- #


def test_conditional_get_sends_etag_and_last_modified() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(304)

    prior = PriorCapture(
        etag='"abc123"', last_modified="Wed, 21 Jul 2026 07:00:00 GMT", body_hash="deadbeef"
    )
    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY, prior=prior)
    assert seen["if-none-match"] == '"abc123"'
    assert seen["if-modified-since"] == "Wed, 21 Jul 2026 07:00:00 GMT"
    assert result.not_modified is True
    assert result.from_cache is True
    assert result.body_hash == "deadbeef"  # prior hash carried through the 304


def test_content_hash_detects_no_change_without_validators() -> None:
    body = b"identical-body"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    import hashlib

    prior = PriorCapture(body_hash=hashlib.sha256(body).hexdigest())
    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY, prior=prior)
    assert result.status_code == 200
    assert result.not_modified is True  # same content hash -> no change
    assert result.body_hash == prior.body_hash


def test_changed_body_is_not_flagged_unchanged() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"brand-new-body")

    prior = PriorCapture(body_hash="0" * 64)
    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY, prior=prior)
    assert result.not_modified is False
    assert result.ok is True


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def test_circuit_breaker_opens_after_threshold_and_short_circuits() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    settings = Settings(connector_failure_threshold=3, scraping_max_retries=0)
    fetcher = _make_fetcher(handler, settings=settings)

    for _ in range(3):
        fetcher.fetch(_URL, policy=_POLICY)
    calls_before = calls["n"]
    assert calls_before == 3  # one request per fetch (no retries)

    blocked = fetcher.fetch(_URL, policy=_POLICY)
    assert blocked.circuit_open is True
    assert blocked.error == "circuit_open"
    assert calls["n"] == calls_before  # short-circuited: NO new request issued


def test_circuit_breaker_resets_on_success() -> None:
    state = {"fail": True}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500) if state["fail"] else httpx.Response(200, text="ok")

    settings = Settings(connector_failure_threshold=3, scraping_max_retries=0)
    fetcher = _make_fetcher(handler, settings=settings)
    fetcher.fetch(_URL, policy=_POLICY)
    fetcher.fetch(_URL, policy=_POLICY)
    state["fail"] = False
    ok = fetcher.fetch(_URL, policy=_POLICY)
    assert ok.ok is True
    # Counter reset -> two more failures do not yet open the circuit.
    state["fail"] = True
    fetcher.fetch(_URL, policy=_POLICY)
    again = fetcher.fetch(_URL, policy=_POLICY)
    assert again.circuit_open is False


# --------------------------------------------------------------------------- #
# Size limit / MIME
# --------------------------------------------------------------------------- #


def test_size_limit_aborts_via_content_length() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 10,
            headers={"content-length": str(10 * 1024 * 1024)},
        )

    settings = Settings(scraping_max_response_mb=1)
    result = _make_fetcher(handler, settings=settings).fetch(
        "https://big.example.com/f", policy=SourcePolicy(allowed_domains=("big.example.com",))
    )
    assert result.ok is False
    assert result.error == "response_too_large"


def test_size_limit_aborts_while_streaming() -> None:
    big = b"y" * (2 * 1024 * 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)  # no content-length header set explicitly

    settings = Settings(scraping_max_response_mb=1)
    result = _make_fetcher(handler, settings=settings).fetch(
        "https://big.example.com/f", policy=SourcePolicy(allowed_domains=("big.example.com",))
    )
    assert result.error == "response_too_large"


def test_mime_validation_rejects_disallowed_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    result = _make_fetcher(handler).fetch(
        _URL, policy=_POLICY, allowed_content_types=("application/json",)
    )
    assert result.ok is False
    assert result.error == "mime_not_allowed"


def test_mime_validation_accepts_allowed_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"a": 1})

    result = _make_fetcher(handler).fetch(
        _URL, policy=_POLICY, allowed_content_types=("application/json",)
    )
    assert result.ok is True


# --------------------------------------------------------------------------- #
# SSRF guard / allowlist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/x",
        "https://10.0.0.5/x",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "https://[::1]/x",
    ],
)
def test_ssrf_guard_rejects_private_and_loopback(url: str) -> None:
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    policy = SourcePolicy(allowed_domains=())  # allowlist empty -> only SSRF guards
    result = _make_fetcher(handler).fetch(url, policy=policy)
    assert result.ok is False
    assert result.error == "ssrf_blocked"
    assert called["n"] == 0  # never issued a request


def test_ssrf_guard_rejects_localhost_hostname() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    fetcher = _make_fetcher(handler, resolver=lambda _host: ["127.0.0.1"])
    result = fetcher.fetch(
        "https://localhost/x", policy=SourcePolicy(allowed_domains=("localhost",))
    )
    assert result.error == "ssrf_blocked"


def test_non_http_scheme_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    result = _make_fetcher(handler).fetch("ftp://prices.example.com/x", policy=_POLICY)
    assert result.error == "scheme_not_allowed"


def test_domain_not_in_allowlist_rejected() -> None:
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    result = _make_fetcher(handler).fetch(
        "https://evil.example.net/x", policy=_POLICY
    )
    assert result.error == "domain_not_allowed"
    assert called["n"] == 0


def test_cross_host_redirect_is_returned_not_followed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other.example.org/y"})

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert result.status_code == 302
    assert result.redirect_location == "https://other.example.org/y"
    assert result.content is None  # not auto-followed


# --------------------------------------------------------------------------- #
# Block-page detection (NEVER solved)
# --------------------------------------------------------------------------- #


def test_block_page_flagged_on_403() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert result.is_block_page is True
    assert result.ok is False
    assert result.error == "block_page"


def test_block_page_flagged_on_429() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert result.is_block_page is True


def test_block_page_flagged_on_challenge_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        html = "<html><body>Please verify you are human. Attention Required!</body></html>"
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    result = _make_fetcher(handler).fetch(
        "https://blocked.example.com/x",
        policy=SourcePolicy(allowed_domains=("blocked.example.com",)),
    )
    assert result.is_block_page is True
    assert result.ok is False  # a captcha wall is not a usable success


def test_block_page_detection_does_not_attempt_to_solve() -> None:
    # Only ONE request is ever made — the fetcher reports the block and stops; it does not
    # loop trying to solve the challenge or hit any challenge/solver endpoint.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="captcha challenge")

    _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Header redaction / cancellation
# --------------------------------------------------------------------------- #


def test_response_headers_are_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="ok",
            headers={
                "set-cookie": "session=supersecret; Path=/",
                "authorization": "Bearer leaky",
                "x-api-key": "leaky-key",
                "content-type": "text/plain",
            },
        )

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY)
    assert result.headers["set-cookie"] == "REDACTED"
    assert result.headers["authorization"] == "REDACTED"
    assert result.headers["x-api-key"] == "REDACTED"
    assert result.headers["content-type"] == "text/plain"  # non-sensitive preserved


def test_cancellation_short_circuits_before_request() -> None:
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    result = _make_fetcher(handler).fetch(_URL, policy=_POLICY, cancel=lambda: True)
    assert result.error == "cancelled"
    assert called["n"] == 0


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_redact_headers_helper_marks_token_like_names() -> None:
    reduced = redact_headers(
        {"X-Auth-Token": "t", "Cookie": "c", "X-Custom-Secret": "s", "Accept": "*/*"}
    )
    assert reduced["X-Auth-Token"] == "REDACTED"
    assert reduced["Cookie"] == "REDACTED"
    assert reduced["X-Custom-Secret"] == "REDACTED"
    assert reduced["Accept"] == "*/*"


def test_detect_block_page_helper() -> None:
    assert detect_block_page(403, {}, b"") is True
    assert detect_block_page(429, {}, b"") is True
    assert detect_block_page(200, {}, b"<html>solve the captcha</html>") is True
    assert detect_block_page(200, {}, b"normal content") is False
