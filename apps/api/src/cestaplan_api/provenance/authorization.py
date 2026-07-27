"""Loader and validator for the future SEALED remediation authorization package.

This phase ships ONLY the loader/validation and the Ed25519 detached-signature check — it never
creates a real production package or key. The package supplies the EXPECTED provenance and expected
counts from a separately-sealed source, so they can never be substituted by the same runtime
variables that provide the OBSERVED values. Every failure is fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUTHORIZATION_SCHEMA_VERSION = 1
MAX_LIFETIME_SECONDS = 24 * 3600  # a short window; a package must expire quickly

_REQUIRED_FIELDS = frozenset({
    "schema_version", "authorization_id", "plan_hash", "main_commit_sha", "alembic_revision",
    "expected_commit_sha", "expected_source_hash", "expected_api_artifact_hash",
    "expected_worker_artifact_hash", "expected_document_hash", "expected_product_price",
    "expected_active_mappings", "generated_at", "expires_at", "operator_reference",
    "backup_expected_sha256", "backup_storage_reference", "authorization_package_hash",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_REVISION_RE = re.compile(r"^[0-9a-z_]{6,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPERATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@ /-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_SUBSTRINGS = ("private key", "begin ", "password=", "secret=", "token=", "://")
_STORAGE_SCHEMES = frozenset({"s3", "gs", "gcs", "b2"})
_STORAGE_OPAQUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_STORAGE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")
_STORAGE_SENSITIVE_RE = re.compile(
    r"(?i)(token|secret|signature|sig=|credential|password|passwd|x-amz-|access[_-]?key|api[_-]?key)")


class AuthorizationError(RuntimeError):
    """Fail-closed authorization failure with a sanitized, stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


def _sanitize_storage_reference(ref: Any) -> str | None:
    """Mirror of the executor's storage-reference contract (§4v5): opaque id with no separators, or
    an allowlisted bucket URI with no userinfo/query/fragment/sensitive parameter."""
    if not isinstance(ref, str) or not (0 < len(ref) <= 200):
        return None
    if _CONTROL_RE.search(ref) or _STORAGE_SENSITIVE_RE.search(ref) or "@" in ref:
        return None
    if "://" not in ref:
        return ref if _STORAGE_OPAQUE_RE.match(ref) else None
    try:
        parsed = urllib.parse.urlsplit(ref)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _STORAGE_SCHEMES:
        return None
    if parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port:
        return None
    host_path = (parsed.netloc or "") + (parsed.path or "")
    return ref if host_path and _STORAGE_PATH_RE.match(host_path) else None


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scan_sensitive(pkg: dict[str, Any]) -> None:
    for v in pkg.values():
        if isinstance(v, str):
            if _CONTROL_RE.search(v):
                raise AuthorizationError("control_char_in_package")
            low = v.lower()
            # allow bucket URIs / opaque ids (validated separately); block anything else secret-like
            if (any(s in low for s in _SECRET_SUBSTRINGS) and not _STORAGE_OPAQUE_RE.match(v)
                    and _sanitize_storage_reference(v) is None):
                raise AuthorizationError("sensitive_data_in_package")


@dataclass(slots=True, frozen=True)
class AuthorizationPackage:
    """A validated, signature-verified authorization package. Feeds ExpectedProvenance + counts."""

    authorization_id: str
    plan_hash: str
    main_commit_sha: str
    alembic_revision: str
    expected_commit_sha: str
    expected_source_hash: str
    expected_api_artifact_hash: str
    expected_worker_artifact_hash: str
    expected_document_hash: str
    expected_product_price: int
    expected_active_mappings: int
    generated_at: datetime
    expires_at: datetime
    operator_reference: str
    backup_expected_sha256: str
    backup_storage_reference: str
    authorization_package_hash: str
    public_key_fingerprint: str

    def expected_provenance_fields(self) -> dict[str, str]:
        """The EXPECTED provenance values (from the sealed package, never from runtime env)."""
        return {
            "commit_sha": self.expected_commit_sha,
            "source_tree_hash": self.expected_source_hash,
            "api_artifact_hash": self.expected_api_artifact_hash,
            "worker_artifact_hash": self.expected_worker_artifact_hash,
            "document_hash": self.expected_document_hash,
        }


def _verify_signature(package_bytes: bytes, signature: bytes,
                      authorized_public_keys: list[str]) -> str:
    if not authorized_public_keys:
        raise AuthorizationError("no_authorized_public_keys")
    if len(signature) != 64:
        raise AuthorizationError("signature_malformed")
    for pk_hex in authorized_public_keys:
        try:
            raw = bytes.fromhex(pk_hex.strip())
        except ValueError:
            continue
        if len(raw) != 32:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, package_bytes)
        except InvalidSignature:
            continue
        return hashlib.sha256(raw).hexdigest()[:16]  # sanitized fingerprint of the trusted key
    raise AuthorizationError("signature_not_authorized")


def _require(cond: bool, code: str, detail: str = "") -> None:
    if not cond:
        raise AuthorizationError(code, detail)


def load_authorization_package(package_bytes: bytes, signature: bytes | str, *,
                               authorized_public_keys: list[str], now: datetime,
                               expected_plan_hash: str) -> AuthorizationPackage:
    """Parse, signature-verify and fully validate a sealed authorization package. Fail-closed on any
    malformed field, unknown field, bad signature, unauthorized key, expiry or plan mismatch."""
    sig = bytes.fromhex(signature.strip()) if isinstance(signature, str) else signature
    fingerprint = _verify_signature(package_bytes, sig, authorized_public_keys)  # before parsing

    try:
        pkg = json.loads(package_bytes)
    except json.JSONDecodeError as exc:
        raise AuthorizationError("package_unparseable") from exc
    _require(isinstance(pkg, dict), "package_not_object")
    keys = set(pkg)
    _require(keys == _REQUIRED_FIELDS, "package_fields_mismatch",
             ",".join(sorted(keys ^ _REQUIRED_FIELDS)))
    _scan_sensitive(pkg)

    _require(pkg["schema_version"] == AUTHORIZATION_SCHEMA_VERSION, "schema_version_unsupported")
    _require(isinstance(pkg["authorization_id"], str)
             and bool(_ID_RE.match(pkg["authorization_id"])), "authorization_id_invalid")
    _require(isinstance(pkg["operator_reference"], str)
             and bool(_OPERATOR_RE.match(pkg["operator_reference"])), "operator_reference_invalid")
    for f in ("plan_hash", "expected_commit_sha", "expected_source_hash",
              "expected_api_artifact_hash", "expected_worker_artifact_hash",
              "expected_document_hash", "backup_expected_sha256"):
        val = pkg[f]
        regex = _COMMIT_RE if f == "expected_commit_sha" else _SHA256_RE
        _require(isinstance(val, str) and bool(regex.match(val)), f"{f}_invalid")
    _require(isinstance(pkg["main_commit_sha"], str)
             and bool(_COMMIT_RE.match(pkg["main_commit_sha"])), "main_commit_sha_invalid")
    _require(isinstance(pkg["alembic_revision"], str)
             and bool(_REVISION_RE.match(pkg["alembic_revision"])), "alembic_revision_invalid")
    for f in ("expected_product_price", "expected_active_mappings"):
        _require(isinstance(pkg[f], int) and not isinstance(pkg[f], bool) and pkg[f] >= 0,
                 f"{f}_invalid")
    _require(_sanitize_storage_reference(pkg["backup_storage_reference"])
             == pkg["backup_storage_reference"], "backup_storage_reference_unsanitized")

    _require(isinstance(pkg["authorization_package_hash"], str)
             and bool(_SHA256_RE.match(pkg["authorization_package_hash"])),
             "authorization_package_hash_invalid")
    recomputed = hashlib.sha256(
        _canonical({k: v for k, v in pkg.items() if k != "authorization_package_hash"}).encode()
    ).hexdigest()
    _require(recomputed == pkg["authorization_package_hash"], "authorization_package_hash_mismatch")

    _require(pkg["plan_hash"] == expected_plan_hash, "plan_hash_mismatch")

    try:
        gen = datetime.fromisoformat(pkg["generated_at"])
        exp = datetime.fromisoformat(pkg["expires_at"])
    except (TypeError, ValueError) as exc:
        raise AuthorizationError("timestamp_invalid") from exc
    _require(gen.tzinfo is not None and exp.tzinfo is not None, "timestamp_not_tz_aware")
    _require(exp > gen, "expiry_before_generation")
    _require((exp - gen).total_seconds() <= MAX_LIFETIME_SECONDS, "lifetime_too_long")
    _require(gen <= now, "package_not_yet_valid")
    _require(now <= exp, "package_expired")

    return AuthorizationPackage(
        authorization_id=pkg["authorization_id"], plan_hash=pkg["plan_hash"],
        main_commit_sha=pkg["main_commit_sha"], alembic_revision=pkg["alembic_revision"],
        expected_commit_sha=pkg["expected_commit_sha"],
        expected_source_hash=pkg["expected_source_hash"],
        expected_api_artifact_hash=pkg["expected_api_artifact_hash"],
        expected_worker_artifact_hash=pkg["expected_worker_artifact_hash"],
        expected_document_hash=pkg["expected_document_hash"],
        expected_product_price=pkg["expected_product_price"],
        expected_active_mappings=pkg["expected_active_mappings"],
        generated_at=gen, expires_at=exp, operator_reference=pkg["operator_reference"],
        backup_expected_sha256=pkg["backup_expected_sha256"],
        backup_storage_reference=pkg["backup_storage_reference"],
        authorization_package_hash=pkg["authorization_package_hash"],
        public_key_fingerprint=fingerprint)
