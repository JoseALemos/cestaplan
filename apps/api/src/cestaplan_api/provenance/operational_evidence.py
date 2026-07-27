"""Operational ceremony evidence + fail-closed file primitives (verify-only ceremony adapter).

``OperationalCeremonyEvidenceV1`` is a strict, single-encoding canonical JSON document naming the
deployed API/worker commits and the real pre-apply backup (path, expected sha256, creation time,
PostgreSQL major, opaque storage reference). It is deliberately WEAK: only OBSERVED input, never a
source of authority. It cannot supply expected provenance/counts, the signed backup identity, the
authorization identity, the package hash or the operator reference — those belong exclusively to the
signed package.

Every ceremony file is read/created fail-closed: descriptor-relative traversal from ``/`` with
``O_DIRECTORY | O_NOFOLLOW`` on each parent (no path component may be a symlink), ``O_NOFOLLOW`` on
the final file, full stat-identity capture (dev/ino/mode/size/mtime_ns/ctime_ns) with a re-open +
re-read to detect any post-first-read change, owner-only permissions where required, and hard size
limits. Errors carry sanitized codes only — never a path. A local backup path is never rendered,
persisted or placed into a request, report or signed package.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat as statmod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cestaplan_api.provenance.authorization import sanitize_storage_reference

OPERATIONAL_EVIDENCE_SCHEMA_VERSION = 1

# Hard size limits (§3v2) — read at most this many bytes; a larger file fails file_too_large.
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PG_MAJOR_RE = re.compile(r"^[1-9][0-9]{0,2}$")
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_TOP_FIELDS = frozenset({"schema_version", "deployed_api_sha", "deployed_worker_sha", "backup"})
_BACKUP_FIELDS = frozenset({
    "path", "expected_sha256", "created_at", "expected_postgres_version", "storage_reference"})

_CHUNK = 1 << 20
PROC_SELF_FD = "/proc/self/fd"


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


# --------------------------------------------------------------------------- #
# Fail-closed, symlink-rejecting, descriptor-relative file primitives (§1/§2/§4)
# --------------------------------------------------------------------------- #
def _flag(name: str) -> int:
    """A required open flag, or fail-closed if the platform lacks it (§1/§4v2)."""
    flag = getattr(os, name, 0)
    if not flag:
        raise CeremonyFileError("open_primitive_unavailable")
    return flag


def _cloexec() -> int:
    return getattr(os, "O_CLOEXEC", 0)


def stat_identity(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """The full identity that must not change while a file is used (§2/§4v2)."""
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _resolve_parent_dirfd(path: str) -> tuple[int, str]:
    """Open the parent directory of ``path`` by walking every component from ``/`` with
    ``O_DIRECTORY | O_NOFOLLOW`` (no component may be a symlink), returning (parent_fd, final_name).
    Absolute path required; ``.``/``..`` components rejected. Caller closes parent_fd."""
    if not isinstance(path, str) or not path:
        raise CeremonyFileError("path_invalid")
    if _CONTROL_RE.search(path):
        raise CeremonyFileError("path_control_char")
    if not os.path.isabs(path):
        raise CeremonyFileError("path_not_absolute")
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise CeremonyFileError("path_invalid")
    dir_flags = os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_NOFOLLOW") | _cloexec()
    dirfd = os.open("/", dir_flags)
    try:
        for comp in parts[:-1]:
            if comp in (".", ".."):
                raise CeremonyFileError("path_traversal_component")
            # lstat first for a clean symlink classification (O_NOFOLLOW|O_DIRECTORY on a symlink
            # can surface as ELOOP or ENOTDIR); O_NOFOLLOW on the open still guards a later swap.
            try:
                st = os.lstat(comp, dir_fd=dirfd)
            except OSError as exc:
                raise CeremonyFileError("path_unreadable") from exc
            if statmod.S_ISLNK(st.st_mode):
                raise CeremonyFileError("symlink_rejected")
            if not statmod.S_ISDIR(st.st_mode):
                raise CeremonyFileError("not_a_directory")
            try:
                nfd = os.open(comp, dir_flags, dir_fd=dirfd)
            except OSError as exc:
                if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
                    raise CeremonyFileError("symlink_rejected") from exc
                raise CeremonyFileError("path_unreadable") from exc
            os.close(dirfd)
            dirfd = nfd
    except BaseException:
        os.close(dirfd)
        raise
    final = parts[-1]
    if final in (".", ".."):
        os.close(dirfd)
        raise CeremonyFileError("path_traversal_component")
    return dirfd, final


def _open_final(parent_fd: int, name: str, flags: int, mode: int = 0o777) -> int:
    try:
        return os.open(name, flags | _flag("O_NOFOLLOW") | _cloexec(), mode, dir_fd=parent_fd)
    except OSError as exc:
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise CeremonyFileError("symlink_rejected") from exc
        if getattr(exc, "errno", None) == errno.EEXIST:
            raise CeremonyFileError("output_exists") from exc
        raise CeremonyFileError("file_unreadable") from exc


def _read_all(fd: int, max_bytes: int | None) -> bytes:
    data = bytearray()
    while chunk := os.read(fd, _CHUNK):
        data.extend(chunk)
        if max_bytes is not None and len(data) > max_bytes:
            raise CeremonyFileError("file_too_large")
    return bytes(data)


def secure_read_bytes(path: str, *, require_owner_only: bool = False,
                      max_bytes: int | None = None) -> bytes:
    """Read a file fully race-safe (§4v2): descriptor-relative traversal (no symlink component),
    ``O_NOFOLLOW`` final open, capture the full stat identity, read, fstat after, then re-open from
    the SAME parent dirfd and re-read — requiring identical identity AND identical bytes, so a
    same-size in-place edit, a truncation, an atomic replace or an inode reuse with different
    metadata all fail closed. Owner-only perms and a size cap are enforced. All fds closed in
    finally. Errors are sanitized codes — never a path."""
    parent_fd, name = _resolve_parent_dirfd(path)
    try:
        fd = _open_final(parent_fd, name, os.O_RDONLY)
        try:
            st0 = os.fstat(fd)
            if statmod.S_ISLNK(st0.st_mode) or not statmod.S_ISREG(st0.st_mode):
                raise CeremonyFileError("not_regular_file")
            if require_owner_only and (st0.st_mode & 0o077):
                raise CeremonyFileError("permissions_too_open")
            if max_bytes is not None and st0.st_size > max_bytes:
                raise CeremonyFileError("file_too_large")
            data = _read_all(fd, max_bytes)
            st1 = os.fstat(fd)
        finally:
            os.close(fd)
        vfd = _open_final(parent_fd, name, os.O_RDONLY)
        try:
            after = os.fstat(vfd)
            data2 = _read_all(vfd, max_bytes)
        finally:
            os.close(vfd)
    finally:
        os.close(parent_fd)
    if (stat_identity(st0) != stat_identity(st1) or stat_identity(st0) != stat_identity(after)
            or data != data2):
        raise CeremonyFileError("file_changed_during_read")
    return data


class SecureDump:
    """A securely-opened backup dump: a held O_RDONLY|O_NOFOLLOW descriptor plus the parent dirfd
    for a race re-open. Used by BackupEvidence so stat, hash and pg_restore all act on ONE inode."""

    __slots__ = ("fd", "name", "parent_fd")

    def __init__(self, parent_fd: int, name: str, fd: int) -> None:
        self.parent_fd = parent_fd
        self.name = name
        self.fd = fd

    def reopen_stat(self) -> os.stat_result:
        vfd = _open_final(self.parent_fd, self.name, os.O_RDONLY)
        try:
            return os.fstat(vfd)
        finally:
            os.close(vfd)

    def close(self) -> None:
        for fd in (self.fd, self.parent_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def secure_open_dump(path: str) -> SecureDump:
    """Open a backup dump fail-closed for single-fd verification (§2v2): reject symlink components,
    ``O_NOFOLLOW`` open, regular file, positive size, owner-only perms. Caller MUST close()."""
    parent_fd, name = _resolve_parent_dirfd(path)
    try:
        fd = _open_final(parent_fd, name, os.O_RDONLY)
    except BaseException:
        os.close(parent_fd)
        raise
    st = os.fstat(fd)
    if statmod.S_ISLNK(st.st_mode) or not statmod.S_ISREG(st.st_mode):
        SecureDump(parent_fd, name, fd).close()
        raise CeremonyFileError("not_regular_file")
    if st.st_size <= 0:
        SecureDump(parent_fd, name, fd).close()
        raise CeremonyFileError("empty_file")
    if st.st_mode & 0o077:
        SecureDump(parent_fd, name, fd).close()
        raise CeremonyFileError("permissions_too_open")
    return SecureDump(parent_fd, name, fd)


# --------------------------------------------------------------------------- #
# Secure, exclusive request-file creation (§1v2)
# --------------------------------------------------------------------------- #
def _repo_root() -> str | None:
    p = os.path.dirname(os.path.abspath(__file__))
    cur = p
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _reject_forbidden_output_location(path: str) -> None:
    ap = os.path.abspath(path)  # traversal already forbids symlink/`..` components
    if ap == "/app" or ap.startswith("/app/"):
        raise CeremonyFileError("output_inside_app")
    root = _repo_root()
    if root is not None and (ap == root or ap.startswith(root + os.sep)):
        raise CeremonyFileError("output_inside_repo")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    total = 0
    while total < len(view):
        n = os.write(fd, view[total:])
        if n <= 0:
            raise CeremonyFileError("short_write")
        total += n


def secure_create_request_file(path: str, payload: bytes) -> None:
    """Create ``path`` EXCLUSIVELY (§1v2): absolute, outside the repo and /app, parent a real
    directory reached descriptor-relative (no symlink component), final open
    ``O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`` mode 0600 (O_EXCL blocks any pre-existing
    target — manifest/evidence/package/signature/trust-root/build-provenance/backup — which is
    NEVER touched). Writes fully (short-write safe), fsyncs the file and parent dir, and
    fstat-verifies regular + exactly 0600 + owner-only + exact size. On failure only THIS run's
    newly-created file is unlinked. Errors are sanitized — never a path."""
    if not isinstance(path, str) or not path or _CONTROL_RE.search(path):
        raise CeremonyFileError("path_invalid")
    if not os.path.isabs(path):
        raise CeremonyFileError("path_not_absolute")
    _reject_forbidden_output_location(path)
    parent_fd, name = _resolve_parent_dirfd(path)
    created = False
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flag("O_NOFOLLOW") | _cloexec(),
                     0o600, dir_fd=parent_fd)
        created = True
        try:
            _write_all(fd, payload)
            os.fsync(fd)
            st = os.fstat(fd)
            if (not statmod.S_ISREG(st.st_mode) or (st.st_mode & 0o777) != 0o600
                    or (st.st_mode & 0o077) or st.st_size != len(payload)):
                raise CeremonyFileError("output_verification_failed")
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.fsync(parent_fd)  # make the new directory entry durable
    except CeremonyFileError:
        if created:
            _unlink_created(parent_fd, name)
        raise
    except OSError as exc:
        if created:
            _unlink_created(parent_fd, name)
        if getattr(exc, "errno", None) == errno.EEXIST:
            raise CeremonyFileError("output_exists") from exc
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise CeremonyFileError("symlink_rejected") from exc
        raise CeremonyFileError("output_unwritable") from exc
    finally:
        os.close(parent_fd)


def _unlink_created(parent_fd: int, name: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(name, dir_fd=parent_fd)  # remove ONLY the file this run created


# --------------------------------------------------------------------------- #
# Strict canonical evidence loader (§3v2)
# --------------------------------------------------------------------------- #
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


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


def load_operational_evidence(path: str) -> OperationalCeremonyEvidence:
    """Load + strictly validate an ``OperationalCeremonyEvidenceV1`` file. Exactly one accepted
    encoding: ``canonical_json(document) + "\\n"`` — extra whitespace/indent, different key order,
    a missing/extra newline, CRLF, a BOM, an alternate unicode form, duplicate keys or NaN/Infinity
    all fail closed. Read via secure_read_bytes (symlink-free, owner-only, race-safe, ≤64 KiB). The
    backup path is validated but NEVER echoed."""
    raw = secure_read_bytes(path, require_owner_only=True, max_bytes=MAX_EVIDENCE_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CeremonyFileError("not_utf8") from exc
    if text.startswith("﻿"):  # a BOM is never part of the canonical encoding
        raise CeremonyFileError("not_canonical")
    try:
        doc = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CeremonyFileError("unparseable") from exc
    if not isinstance(doc, dict):
        raise CeremonyFileError("not_object")
    # The one valid encoding: canonical JSON + a single trailing newline, byte-for-byte.
    if _canonical(doc) + "\n" != text:
        raise CeremonyFileError("not_canonical")
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


def stream_sha256_fd(fd: int) -> str:
    """SHA-256 over the whole file at ``fd``, from offset 0, without loading it into memory."""
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while chunk := os.read(fd, _CHUNK):
        h.update(chunk)
    return h.hexdigest()
