"""Deterministic, fail-closed file manifests for build provenance.

A manifest is a canonical description of a set of runtime files: a POSIX relative path, the SHA-256
of the content, the byte size, and the git-tracked executable bit. It contains NO mtimes, owners,
absolute paths or any other non-reproducible metadata, so the same source tree always yields the
same manifest and the same hash — byte-for-byte, on any machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


def _hash_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _add_file(base: Path, path: Path, entries: dict[str, dict[str, Any]]) -> None:
    # Symlinks escaping the tree are rejected; an in-tree symlink is hashed by its target.
    if path.is_symlink():
        resolved = path.resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ProvenanceError("symlink_escapes_tree",
                                  path.name) from exc  # sanitized: name only
    try:
        rel = path.resolve().relative_to(base).as_posix()
    except ValueError as exc:
        raise ProvenanceError("path_escapes_tree", path.name) from exc
    if _CONTROL_RE.search(rel):
        raise ProvenanceError("control_char_in_path", "")
    if rel in entries:
        raise ProvenanceError("duplicate_path", rel)
    size_before = path.stat().st_size
    digest, size = _hash_and_size(path)
    if size != size_before or size != path.stat().st_size:
        raise ProvenanceError("file_changed_during_scan", rel)
    entries[rel] = {
        "path": rel, "sha256": digest, "size": size,
        "executable": bool(path.stat().st_mode & 0o100),  # git-tracked user-exec bit only
    }


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
