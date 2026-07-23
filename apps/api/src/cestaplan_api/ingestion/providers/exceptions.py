"""Exceptions for the price-catalog provider layer (FASE 1)."""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for provider-layer failures."""


class NotSupportedError(ProviderError):
    """Raised when a provider does not support a requested operation."""


class ProviderAuthError(ProviderError):
    """Authentication/authorization failure (401/403) — never leaks credentials."""


class ProviderRateLimited(ProviderError):
    """Rate limited (429). Carries an optional retry-after hint in seconds."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderQuotaExceeded(ProviderError):
    """A configured daily-run / daily-cost / per-run quota was reached."""


class ProviderResponseError(ProviderError):
    """Malformed/unexpected response (HTML when JSON expected, truncated, block page)."""


__all__ = [
    "NotSupportedError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderQuotaExceeded",
    "ProviderRateLimited",
    "ProviderResponseError",
]
