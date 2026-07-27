"""Operational ceremony evidence — the OBSERVED runtime facts an operator supplies to the
verify-only authorization ceremony.

``OperationalCeremonyEvidenceV1`` is a strict, canonical JSON document naming the deployed
API/worker commits and the real pre-apply backup (path, expected sha256, creation time, PostgreSQL
major, opaque storage reference). It is deliberately WEAK: only OBSERVED input, never a source of
authority. It cannot supply expected provenance, expected counts, the signed backup identity, the
authorization identity, the package hash or the operator reference — those belong exclusively to the
signed authorization package. The evidence file is read fail-closed (O_NOFOLLOW, regular-file, no
group/other permissions, race-safe), every failure a sanitized code, and the local backup PATH is
never printed, persisted or placed into any signed package or sanitized report.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat as statmod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cestaplan_api.provenance.authorization import sanitize_storage_reference

OPERATIONAL_EVIDENCE_SCHEMA_VERSION = 1

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PG_MAJOR_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_TOP_FIELDS = frozenset({"schema_version", "deployed_api_sha", "deployed_worker_sha", "backup"})
_BACKUP_FIELDS = frozenset({
    "path", "expected_sha256", "created_at", "expected_postgres_version", "storage_reference"})

_CHUNK = 1 << 20


class CeremonyFileError(RuntimeError):
    """A fail-closed ceremony-file failure with a sanitized, stable ``code`` (never a path)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class OperationalCeremonyEvidence:
    """Parsed OBSERVED evidence. The local backup ``path`` is held only to build BackupEvidence and
    is NEVER rendered (``repr=False``); it must never reach a report, audit or signed package."""

    deployed_api_sha: str
    deployed_worker_sha: str
    backup_path: str = field(repr=False)
    backup_expected_sha256: str
    backup_created_at: datetime
    backup_expected_postgres_version: str
    backup_storage_reference: str


def _nofollow_flag() -> int:
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:  # never fall back to a symlink-following open (§10)
        raise CeremonyFileError("o_nofollow_unavailable")
    return flag


def secure_read_bytes(path: str, *, require_owner_only: bool = False) -> bytes:
    """Read a file fail-closed: reject symlinks (O_NOFOLLOW), require a regular file, optionally
    require no group/other permission bits, and detect any change/truncation/substitution mid-read
    via fstat before/after + an O_NOFOLLOW re-open. Errors carry sanitized codes only, never a path.
    """
    if not isinstance(path, str) or not path:
        raise CeremonyFileError("path_invalid")
    if _CONTROL_RE.search(path):
        raise CeremonyFileError("path_control_char")
    flags = os.O_RDONLY | _nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise CeremonyFileError("symlink_rejected") from exc
        raise CeremonyFileError("file_unreadable") from exc
    try:
        st0 = os.fstat(fd)
        if statmod.S_ISLNK(st0.st_mode) or not statmod.S_ISREG(st0.st_mode):
            raise CeremonyFileError("not_regular_file")
        if require_owner_only and (st0.st_mode & 0o077):
            raise CeremonyFileError("permissions_too_open")
        data = bytearray()
        while chunk := os.read(fd, _CHUNK):
            data.extend(chunk)
        st1 = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        vfd = os.open(path, flags)
        try:
            after = os.fstat(vfd)
        finally:
            os.close(vfd)
    except OSError as exc:
        raise CeremonyFileError("file_changed_during_read") from exc
    if (st0.st_ino != st1.st_ino or st0.st_size != st1.st_size
            or st0.st_mtime_ns != st1.st_mtime_ns or st0.st_ctime_ns != st1.st_ctime_ns
            or after.st_ino != st0.st_ino or not statmod.S_ISREG(after.st_mode)
            or after.st_size != len(data)):
        raise CeremonyFileError("file_changed_during_read")
    return bytes(data)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise CeremonyFileError("duplicate_key")
        seen[k] = v
    return seen


def _reject_constant(_c: str) -> Any:  # NaN / Infinity / -Infinity
    raise CeremonyFileError("non_finite_number")


def _parse_rfc3339_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not _RFC3339_UTC_RE.match(value):
        raise CeremonyFileError("created_at_invalid")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CeremonyFileError("created_at_invalid") from exc
    if dt.tzinfo is None or dt.utcoffset() != UTC.utcoffset(None):
        raise CeremonyFileError("created_at_invalid")
    return dt


def _reject_non_finite(obj: Any) -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        raise CeremonyFileError("non_finite_number")
    if isinstance(obj, dict):
        for v in obj.values():
            _reject_non_finite(v)
    elif isinstance(obj, list):
        for v in obj:
            _reject_non_finite(v)


def load_operational_evidence(path: str) -> OperationalCeremonyEvidence:
    """Load + strictly validate an ``OperationalCeremonyEvidenceV1`` file. Fail-closed on unknown or
    missing fields, malformed sha/commit/timestamp/version/reference, a relative or control-char
    backup path, a symlink, world/group-accessible permissions or a mid-read change. The backup path
    is validated but NEVER echoed."""
    raw = secure_read_bytes(path, require_owner_only=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CeremonyFileError("not_utf8") from exc
    try:
        doc = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CeremonyFileError("unparseable") from exc
    if not isinstance(doc, dict):
        raise CeremonyFileError("not_object")
    _reject_non_finite(doc)
    if set(doc) != _TOP_FIELDS:
        raise CeremonyFileError("fields_mismatch")
    if doc["schema_version"] != OPERATIONAL_EVIDENCE_SCHEMA_VERSION:
        raise CeremonyFileError("schema_version_unsupported")
    for f in ("deployed_api_sha", "deployed_worker_sha"):
        if not isinstance(doc[f], str) or not _COMMIT_RE.match(doc[f]):
            raise CeremonyFileError(f"{f}_invalid")
    b = doc["backup"]
    if not isinstance(b, dict) or set(b) != _BACKUP_FIELDS:
        raise CeremonyFileError("backup_fields_mismatch")
    bpath = b["path"]
    if (not isinstance(bpath, str) or not bpath or _CONTROL_RE.search(bpath)
            or not os.path.isabs(bpath)):
        raise CeremonyFileError("backup_path_invalid")
    if not isinstance(b["expected_sha256"], str) or not _SHA256_RE.match(b["expected_sha256"]):
        raise CeremonyFileError("backup_expected_sha256_invalid")
    created = _parse_rfc3339_utc(b["created_at"])
    pv = b["expected_postgres_version"]
    if not isinstance(pv, str) or not _PG_MAJOR_RE.match(pv):
        raise CeremonyFileError("backup_expected_postgres_version_invalid")
    ref = b["storage_reference"]
    if not isinstance(ref, str) or sanitize_storage_reference(ref) != ref:
        raise CeremonyFileError("backup_storage_reference_invalid")
    return OperationalCeremonyEvidence(
        deployed_api_sha=doc["deployed_api_sha"],
        deployed_worker_sha=doc["deployed_worker_sha"],
        backup_path=bpath,
        backup_expected_sha256=b["expected_sha256"],
        backup_created_at=created,
        backup_expected_postgres_version=pv,
        backup_storage_reference=ref)
