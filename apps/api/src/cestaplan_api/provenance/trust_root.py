"""Immutable authorization trust-root loader (feat immutable-build-provenance v2).

The set of authorized Ed25519 public keys is baked into the image as a canonical JSON file
(``authorization-trust-root.json``) and included in every provenance scope hash — it can NEVER be
substituted by a runtime environment variable. This PR ships an EMPTY trust-root (no real key), so
authorization verification is fail-closed and ``apply_ready`` stays false. A separate future PR adds
the real public key; the private key never lives in repo, CI or Railway.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

TRUST_ROOT_SCHEMA_VERSION = 1
_TRUST_ROOT_FIELDS = frozenset({"schema_version", "authorized_ed25519_public_keys"})
_PUBKEY_RE = re.compile(r"^[0-9a-f]{64}$")  # 32 bytes, lowercase hex


class TrustRootError(RuntimeError):
    """Fail-closed trust-root failure with a sanitized, stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def trust_root_hash(path: str | Path) -> str:
    """SHA-256 of the trust-root file's exact bytes (matches the manifest content hash)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_trust_root(raw: bytes) -> list[str]:
    """Validate a canonical trust-root document and return its authorized public keys (lowercase
    32-byte hex). Fail-closed on bad UTF-8, non-canonical bytes, unknown/missing fields, malformed
    or duplicate keys."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrustRootError("trust_root_not_utf8") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrustRootError("trust_root_unparseable") from exc
    if not isinstance(doc, dict) or set(doc) != _TRUST_ROOT_FIELDS:
        raise TrustRootError("trust_root_fields_mismatch")
    if doc["schema_version"] != TRUST_ROOT_SCHEMA_VERSION:
        raise TrustRootError("trust_root_schema_unsupported")
    keys = doc["authorized_ed25519_public_keys"]
    if not isinstance(keys, list):
        raise TrustRootError("trust_root_keys_not_list")
    seen: set[str] = set()
    for k in keys:
        if not isinstance(k, str) or not _PUBKEY_RE.match(k):
            raise TrustRootError("trust_root_key_malformed")
        if k in seen:
            raise TrustRootError("trust_root_duplicate_key")
        seen.add(k)
    # Bytes must be exactly canonical (no alternate whitespace / ordering / trailing newline drift).
    if _canonical(doc) + "\n" != text:
        raise TrustRootError("trust_root_not_canonical")
    return list(keys)


def load_trust_root(path: str | Path) -> list[str]:
    """Load + validate the baked trust-root from ``path``. Fail-closed if unreadable."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise TrustRootError("trust_root_unreadable") from exc
    return parse_trust_root(raw)
