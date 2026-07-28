"""Fail-closed verification of the pinned pg_restore binary (spec §6/§7).

These tests exercise the REAL apply_tool._verify_pg_restore_binary — no test double. The strict
root-owned positive path and the wrong-major path require root (the binary must be uid 0), so they
are skipped on a non-root runner; every negative gate that is uid-independent runs everywhere. The
backup happy paths in the other ingestion suites relax only the root-owned gate via a test double;
the strict gate lives here and in CI's image-runtime job (real root-owned client, app user).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cestaplan_api.tools import apply_history_lane_remediation as apply_tool

IS_ROOT = os.geteuid() == 0
root_only = pytest.mark.skipif(
    not IS_ROOT, reason="strict root-owned binary semantics require root")


def _fake(tmp_path: Path, *, major: str = "18", mode: int = 0o755,
          name: str = "pg_restore") -> tuple[str, str]:
    body = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "pg_restore (PostgreSQL) {major}.4";;\n'
        f'  --list) echo "; Dumped from database version: {major}";;\n'
        "  *) exit 1;;\n"
        "esac\nexit 0\n")
    p = tmp_path / name
    p.write_text(body)
    os.chmod(p, mode)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def _verify(path: str, sha: str | None):
    return apply_tool._verify_pg_restore_binary(
        sha, expected_major=apply_tool.PG_RESTORE_REQUIRED_MAJOR, path=path)


# --------------------------------------------------------------------------- #
# uid-independent negatives — run on every runner (root or not)
# --------------------------------------------------------------------------- #
def test_absent_path_fails(tmp_path: Path) -> None:
    ok, resolved = _verify(str(tmp_path / "nope"), "d" * 64)
    assert ok is False and resolved is None


def test_none_sha_fails(tmp_path: Path) -> None:
    path, _ = _fake(tmp_path)
    ok, resolved = _verify(path, None)
    assert ok is False and resolved is None


def test_malformed_sha_fails(tmp_path: Path) -> None:
    path, _ = _fake(tmp_path)
    ok, _resolved = _verify(path, "not-a-sha")
    assert ok is False


def test_sha_mismatch_fails(tmp_path: Path) -> None:
    path, _sha = _fake(tmp_path)
    ok, resolved = _verify(path, "d" * 64)  # right binary, wrong expected hash
    assert ok is False and resolved is None


def test_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    ok, _resolved = _verify(str(d), "d" * 64)
    assert ok is False


def test_symlink_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path)
    link = tmp_path / "link_pg_restore"
    os.symlink(path, link)  # O_NOFOLLOW must refuse to open through the symlink
    ok, _resolved = _verify(str(link), sha)
    assert ok is False


def test_group_writable_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path, mode=0o775)  # group-writable -> app-modifiable -> reject
    ok, _resolved = _verify(path, sha)
    assert ok is False


def test_world_writable_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path, mode=0o757)  # other-writable -> reject
    ok, _resolved = _verify(path, sha)
    assert ok is False


# --------------------------------------------------------------------------- #
# strict root-owned semantics — require an actual root-owned binary
# --------------------------------------------------------------------------- #
@root_only
def test_valid_root_owned_binary_passes(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path)  # created by root -> uid 0, 0755
    ok, resolved = _verify(path, sha)
    assert ok is True and resolved == path


@root_only
def test_major_17_binary_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path, major="17")  # correct sha, wrong major
    ok, resolved = _verify(path, sha)
    assert ok is False and resolved is None


@root_only
def test_major_19_binary_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path, major="19")
    ok, resolved = _verify(path, sha)
    assert ok is False and resolved is None


@root_only
def test_non_root_owned_binary_is_rejected(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path)
    # chown to a non-root uid (nobody=65534) so ownership, not perms, is the sole failing gate.
    os.chown(path, 65534, 65534)
    ok, resolved = _verify(path, sha)
    assert ok is False and resolved is None


# --------------------------------------------------------------------------- #
# path resolution
# --------------------------------------------------------------------------- #
def test_default_pg_restore_path_is_the_pinned_major_18_binary(monkeypatch) -> None:
    monkeypatch.delenv("CESTAPLAN_PG_RESTORE_PATH", raising=False)
    assert apply_tool._pg_restore_path() == "/usr/lib/postgresql/18/bin/pg_restore"
    assert apply_tool.PG_RESTORE_REQUIRED_MAJOR == "18"


def test_pg_restore_path_env_override(monkeypatch, tmp_path: Path) -> None:
    target = str(tmp_path / "custom_pg_restore")
    monkeypatch.setenv("CESTAPLAN_PG_RESTORE_PATH", target)
    assert apply_tool._pg_restore_path() == target
