"""Password hashing, opaque session tokens and login rate limiting.

Security contract (see docs/SECURITY.md §1 and docs/PRIVACY.md §6):

- Passwords are hashed with **Argon2id** (``argon2-cffi``). Never stored in clear,
  never with a fast unsalted hash. Cost parameters live in :data:`_password_hasher`.
- Session identifiers are **opaque**: a high-entropy random string is handed to the
  client, and only its SHA-256 hash is persisted (``UserSession.token_hash``). The raw
  token never touches the database or the logs.
- IPs used for security purposes are stored **hashed** (never in clear), keyed with the
  application ``SESSION_SECRET`` so they cannot be correlated across deployments.
- A small in-memory rate limiter throttles login attempts per ``(email, ip)`` to blunt
  credential-stuffing and brute force. It is intentionally simple; a multi-process
  deployment would move this to a shared store, but for the vertical slice (single API
  process) an in-memory sliding window is sufficient and has no external dependency.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError

from cestaplan_api.config import get_settings

# Argon2id hasher with library defaults (type=ID). Parameters are reviewed periodically
# per docs/SECURITY.md §1.1; argon2-cffi's defaults are OWASP-aligned.
_password_hasher = PasswordHasher()

# Number of random bytes behind a session token. 32 bytes -> ~43 url-safe chars.
_SESSION_TOKEN_BYTES = 32


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Return an Argon2id PHC-formatted hash for ``password``."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify ``password`` against a stored Argon2id hash. Never raises."""
    try:
        return _password_hasher.verify(password_hash, password)
    except Exception:
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated cost parameters and should be upgraded."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


# --------------------------------------------------------------------------- #
# Opaque session tokens
# --------------------------------------------------------------------------- #
def hash_token(raw_token: str) -> bytes:
    """SHA-256 the raw session token into the bytes persisted as ``token_hash``.

    SHA-256 (no KDF) is correct here: the token is already high-entropy and random, so
    it is not brute-forceable, unlike a human password.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def new_session_token() -> tuple[str, bytes]:
    """Return ``(raw_token, token_hash)``. Hand ``raw_token`` to the client only."""
    raw_token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
    return raw_token, hash_token(raw_token)


def new_csrf_token() -> str:
    """High-entropy anti-CSRF token for the double-submit cookie pattern."""
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def hash_ip(ip: str | None) -> bytes | None:
    """Hash a client IP for audit storage, keyed with SESSION_SECRET. ``None``-safe."""
    if not ip:
        return None
    keyed = f"{get_settings().session_secret}:{ip}".encode()
    return hashlib.sha256(keyed).digest()


# --------------------------------------------------------------------------- #
# Login rate limiting
# --------------------------------------------------------------------------- #
class LoginRateLimiter:
    """In-memory sliding-window rate limiter keyed by an arbitrary string.

    Not shared across processes; sufficient for the single-process slice. Failed
    attempts are recorded and expire after ``window_seconds``; a successful login
    resets the counter for that key.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        kept = [t for t in self._attempts.get(key, []) if t > cutoff]
        if kept:
            self._attempts[key] = kept
        else:
            self._attempts.pop(key, None)
        return kept

    def is_limited(self, key: str) -> bool:
        """True when ``key`` has reached the maximum attempts inside the window."""
        with self._lock:
            now = time.monotonic()
            return len(self._prune(key, now)) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        """Record one failed attempt for ``key``."""
        with self._lock:
            now = time.monotonic()
            attempts = self._prune(key, now)
            attempts.append(now)
            self._attempts[key] = attempts

    def reset(self, key: str) -> None:
        """Clear all recorded attempts for ``key`` (call on successful login)."""
        with self._lock:
            self._attempts.pop(key, None)

    def reset_all(self) -> None:
        """Clear every counter (used by tests for isolation)."""
        with self._lock:
            self._attempts.clear()


# Module-level singleton shared by the auth router.
login_rate_limiter = LoginRateLimiter()
