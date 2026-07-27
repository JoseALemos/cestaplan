"""Deterministic, fail-closed file manifests for build provenance.

A manifest is a canonical description of a set of runtime files: a POSIX relative path, the SHA-256
of the content, and the byte size. It contains NO mtimes, owners, absolute paths or executable bit
(the executable bit is deliberately OMITTED — it is not reproducible across git checkout vs image
copy without a git-mode source, so the contract does not depend on it), so the same source tree
always yields the same manifest and the same hash — byte-for-byte, on any machine.

Hashing is race-safe and TOCTOU-free: symlinks are rejected outright via ``O_NOFOLLOW`` (no
is_symlink() pre-check to race), and each file is read through a single descriptor with an ``fstat``
before and after plus an ``O_NOFOLLOW`` re-open, so a same-size in-place mutation, an atomic
replacement (regular file or symlink swap), a type change or a truncation during the scan all fail
closed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat as statmod
from pathlib import Path
from typing import Any


class ProvenanceError(RuntimeError):
    """A fail-closed provenance failure with a sanitized, stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# Directories excluded everywhere they appear (VCS, caches, deps, envs, logs, backups, editors).
_EXCLUDED_DIRS = frozenset({
    ".git", ".github", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".turbo", ".next", "dist", "build", ".cache", "logs",
    "backups", ".idea", ".vscode", "htmlcov", ".tox", ".gitignore.d",
})
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log", ".tmp", ".swp", ".swo", ".dump", ".sqlite",
                      ".sqlite3", ".coverage", ".orig", ".rej")
_EXCLUDED_NAMES = frozenset({
    "build-provenance.json", "build-provenance.sha256", "build-provenance.json.sha256",
    ".DS_Store", ".coverage", ".env",
})
_EXCLUDED_NAME_PATTERNS = (
    re.compile(r"^\.env(\..+)?$"),          # .env, .env.local, .env.production, ...
    re.compile(r".*\.secret$"), re.compile(r".*\.key$"), re.compile(r".*\.pem$"),
    re.compile(r"^secrets?(\..+)?$"),
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CHUNK = 1 << 20


def _dir_excluded(name: str) -> bool:
    return name in _EXCLUDED_DIRS


def file_excluded(name: str) -> bool:
    """A file basename is excluded (secrets, caches, generated non-runtime artifacts)."""
    if name in _EXCLUDED_NAMES or name.endswith(_EXCLUDED_SUFFIXES):
        return True
    return any(p.match(name) for p in _EXCLUDED_NAME_PATTERNS)


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int, int]:
    """The identity fields that must not change while a file is hashed."""
    return (st.st_size, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns)


def _nofollow_flag() -> int:
    """The O_NOFOLLOW open flag, resolved dynamically so tests can remove it. If the platform lacks
    it we CANNOT open files without following symlinks, so we fail closed rather than silently fall
    back to a symlink-following O_RDONLY (spec §3v4)."""
    flag = getattr(os, "O_NOFOLLOW", 0)
    if not flag:
        raise ProvenanceError("o_nofollow_unavailable", "")
    return flag


def _add_file(base: Path, path: Path, entries: dict[str, dict[str, Any]]) -> None:
    # rel is the WALK-relative path. Symlinks are rejected OUTRIGHT for these production scopes (no
    # need to follow any) — O_NOFOLLOW makes the check atomic with the open, closing the TOCTOU
    # window between an is_symlink() pre-check and the open (spec §8).
    rel = path.relative_to(base).as_posix()
    if _CONTROL_RE.search(rel):
        raise ProvenanceError("control_char_in_path", "")
    if rel in entries:
        raise ProvenanceError("duplicate_path", rel)
    flags = os.O_RDONLY | _nofollow_flag()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) in (errno.ELOOP, errno.EMLINK):
            raise ProvenanceError("symlink_rejected", path.name) from exc  # name only
        raise ProvenanceError("file_unreadable", rel) from exc
    try:
        st0 = os.fstat(fd)
        if statmod.S_ISLNK(st0.st_mode) or not statmod.S_ISREG(st0.st_mode):
            raise ProvenanceError("irregular_file", rel)  # only regular files are hashed
        h = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
            size += len(chunk)
        st1 = os.fstat(fd)
    finally:
        os.close(fd)
    # An atomic replacement of the path during the hash is caught by re-opening with O_NOFOLLOW: a
    # swap to a symlink fails ELOOP; a swap to a new regular file changes the inode.
    try:
        vfd = os.open(path, flags)
        try:
            after = os.fstat(vfd)
        finally:
            os.close(vfd)
    except OSError as exc:
        raise ProvenanceError("file_changed_during_scan", rel) from exc
    if (_stat_key(st0) != _stat_key(st1) or size != st1.st_size
            or after.st_ino != st0.st_ino or not statmod.S_ISREG(after.st_mode)):
        raise ProvenanceError("file_changed_during_scan", rel)
    entries[rel] = {"path": rel, "sha256": h.hexdigest(), "size": size}


def build_manifest(base: str | os.PathLike[str], includes: list[str]) -> list[dict[str, Any]]:
    """Return a canonical, lexicographically-sorted manifest of the files under ``base`` reachable
    from ``includes`` (each a base-relative dir or file). Fail-closed on missing includes, escaping
    symlinks, duplicate paths, control characters, or files that change mid-scan."""
    base_path = Path(base).resolve()
    if not base_path.is_dir():
        raise ProvenanceError("base_not_a_directory", "")
    _nofollow_flag()  # fail closed on platforms without O_NOFOLLOW (spec §3v4)
    entries: dict[str, dict[str, Any]] = {}
    for inc in includes:
        target = (base_path / inc)
        # §3v4: NEVER call exists()/is_file() first — those FOLLOW symlinks. Use lstat so an include
        # root that is a symlink (to a file OR a directory) is rejected outright, not followed.
        try:
            st = os.lstat(target)
        except FileNotFoundError as exc:
            raise ProvenanceError("include_missing", inc) from exc
        except OSError as exc:
            raise ProvenanceError("include_unreadable", inc) from exc
        if statmod.S_ISLNK(st.st_mode):
            raise ProvenanceError("symlink_rejected", inc)
        if statmod.S_ISREG(st.st_mode):
            _add_file(base_path, target, entries)
            continue
        if not statmod.S_ISDIR(st.st_mode):
            raise ProvenanceError("irregular_file", inc)
        for root, dirs, files in os.walk(target, followlinks=False):
            kept = sorted(d for d in dirs if not _dir_excluded(d))
            # §3v4: every enumerated (non-excluded) subdirectory must be a REAL directory. os.walk
            # with followlinks=False would silently NOT recurse a directory symlink; instead reject
            # it explicitly (lstat / follow_symlinks=False) — never skip it silently.
            for d in kept:
                dst = os.lstat(os.path.join(root, d))
                if statmod.S_ISLNK(dst.st_mode) or not statmod.S_ISDIR(dst.st_mode):
                    raise ProvenanceError("symlink_rejected", d)
            dirs[:] = kept
            for fn in sorted(files):
                if file_excluded(fn):
                    continue
                _add_file(base_path, Path(root) / fn, entries)
    return [entries[k] for k in sorted(entries)]


def manifest_hash(manifest: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of a manifest (sorted keys, stable unicode)."""
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
