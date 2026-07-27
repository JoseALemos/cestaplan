"""Deterministic, fail-closed file manifests for build provenance.

A manifest is a canonical description of a set of runtime files: a POSIX relative path, the SHA-256
of the content, and the byte size. It contains NO mtimes, owners, absolute paths or executable bit
(the executable bit is deliberately OMITTED — it is not reproducible across git checkout vs image
copy without a git-mode source, so the contract does not depend on it), so the same source tree
always yields the same manifest and the same hash — byte-for-byte, on any machine.

Hashing is race-safe: each file is read through a single file descriptor, with an ``fstat`` before
and after the read plus a follow ``stat`` of the path, so a same-size in-place mutation, an atomic
replacement, a type change or a truncation during the scan all fail closed.
"""

from __future__ import annotations

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
    "build-provenance.json", "build-provenance.sha256", ".DS_Store", ".coverage", ".env",
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


def _add_file(base: Path, path: Path, entries: dict[str, dict[str, Any]]) -> None:
    # rel is the WALK-relative path (not resolved), so an in-tree symlink keeps its own name.
    rel = path.relative_to(base).as_posix()
    if _CONTROL_RE.search(rel):
        raise ProvenanceError("control_char_in_path", "")
    if rel in entries:
        raise ProvenanceError("duplicate_path", rel)
    # A symlink whose target escapes the tree is rejected; an in-tree symlink is hashed by target.
    if path.is_symlink():
        try:
            path.resolve().relative_to(base)
        except ValueError as exc:
            raise ProvenanceError("symlink_escapes_tree", path.name) from exc  # name only
    fd = os.open(path, os.O_RDONLY)  # follows an in-tree symlink to its target
    try:
        st0 = os.fstat(fd)
        if not statmod.S_ISREG(st0.st_mode):
            raise ProvenanceError("irregular_file", rel)
        h = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
            size += len(chunk)
        st1 = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after = os.stat(path)  # follow: detects an atomic replacement of the target
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
    entries: dict[str, dict[str, Any]] = {}
    for inc in includes:
        target = (base_path / inc)
        if not target.exists():
            raise ProvenanceError("include_missing", inc)
        if target.is_file():
            _add_file(base_path, target, entries)
            continue
        for root, dirs, files in os.walk(target, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _dir_excluded(d))
            for fn in sorted(files):
                if file_excluded(fn):
                    continue
                _add_file(base_path, Path(root) / fn, entries)
    return [entries[k] for k in sorted(entries)]


def manifest_hash(manifest: list[dict[str, Any]]) -> str:
    """SHA-256 over the canonical JSON of a manifest (sorted keys, stable unicode)."""
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
