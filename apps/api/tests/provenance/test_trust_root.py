"""Immutable authorization trust-root tests (feat immutable-build-provenance v2). The shipped
trust-root holds EXACTLY the one enrolled remediation public key; these validate the loader, its
fail-closed behaviour, and the enrolled key's fingerprint."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from cestaplan_api.provenance import trust_root as tr

_REPO_TRUST_ROOT = Path(__file__).resolve().parents[2] / "authorization-trust-root.json"
_KEY = "aa" * 32  # a well-formed 32-byte hex public key (not a real key)
# The enrolled remediation authorization public key (PUBLIC material only) + its fingerprint,
# computed by the project algorithm sha256(pubkey_bytes)[:16]. No private key exists in this repo.
_ENROLLED_KEY = "4f08561609a89d7abae0f037bfc726d94e65e92f3396451c17a231783030f0f9"
_ENROLLED_FINGERPRINT = "e757d17a1d055212"


def test_shipped_trust_root_holds_exactly_the_enrolled_key() -> None:
    keys = tr.load_trust_root(_REPO_TRUST_ROOT)
    assert keys == [_ENROLLED_KEY]  # exactly one authorized key
    raw = bytes.fromhex(keys[0])
    assert len(raw) == 32  # a 32-byte Ed25519 public key
    assert hashlib.sha256(raw).hexdigest()[:16] == _ENROLLED_FINGERPRINT


def _doc(keys: list[str]) -> bytes:
    import json
    return (json.dumps({"schema_version": 1, "authorized_ed25519_public_keys": keys},
                       sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_parse_valid_with_keys() -> None:
    assert tr.parse_trust_root(_doc([_KEY])) == [_KEY]


def test_reject_non_canonical() -> None:
    import json
    raw = (json.dumps({"schema_version": 1, "authorized_ed25519_public_keys": []},
                      indent=2) + "\n").encode()
    with pytest.raises(tr.TrustRootError) as ei:
        tr.parse_trust_root(raw)
    assert ei.value.code == "trust_root_not_canonical"


def test_reject_unknown_field() -> None:
    import json
    raw = (json.dumps({"schema_version": 1, "authorized_ed25519_public_keys": [], "x": 1},
                      sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(tr.TrustRootError) as ei:
        tr.parse_trust_root(raw)
    assert ei.value.code == "trust_root_fields_mismatch"


def test_reject_malformed_key() -> None:
    with pytest.raises(tr.TrustRootError) as ei:
        tr.parse_trust_root(_doc(["ZZ" * 32]))
    assert ei.value.code == "trust_root_key_malformed"


def test_reject_duplicate_key() -> None:
    with pytest.raises(tr.TrustRootError) as ei:
        tr.parse_trust_root(_doc([_KEY, _KEY]))
    assert ei.value.code == "trust_root_duplicate_key"


def test_reject_bad_utf8() -> None:
    with pytest.raises(tr.TrustRootError) as ei:
        tr.parse_trust_root(b"\xff\xfe")
    assert ei.value.code == "trust_root_not_utf8"


def test_hash_matches_file_bytes() -> None:
    assert tr.trust_root_hash(_REPO_TRUST_ROOT) == \
        hashlib.sha256(_REPO_TRUST_ROOT.read_bytes()).hexdigest()
