"""Fail-closed resolution + TOCTOU-free execution of the pinned pg_restore (spec §6/§7 v2).

These tests exercise the REAL apply_tool.open_verified_pg_restore / VerifiedPgRestore. The strict
root-owned + secure-ancestor gates cannot pass for a /tmp fixture (/tmp is world-writable and a CI
runner is non-root), so the happy paths run with apply_tool._PG_REQUIRE_ROOT_OWNED relaxed (which is
NEVER honored in cloud/production) and the strict gates are asserted as REJECTIONS here + verified
for real against the /usr closure by CI's image-runtime job. No private key or production data.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from cestaplan_api.tools import apply_history_lane_remediation as apply_tool

IS_ROOT = os.geteuid() == 0
root_only = pytest.mark.skipif(not IS_ROOT, reason="requires root (chown / root-owned fixtures)")


def _fake_body(major: str = "18", marker: str = "") -> str:
    return (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version) echo "pg_restore (PostgreSQL) {major}.4{marker}";;\n'
        f'  --list) echo "; Dumped from database version: {major}";;\n'
        "  *) exit 1;;\n"
        "esac\nexit 0\n")


def _write_exec(p: Path, body: str, mode: int = 0o755) -> tuple[str, str]:
    p.write_text(body)
    os.chmod(p, mode)
    return str(p), hashlib.sha256(p.read_bytes()).hexdigest()


def _libfile(tmp_path: Path, name: str = "lib.so", data: bytes = b"libdata") -> tuple[str, str]:
    p = tmp_path / name
    p.write_bytes(data)
    os.chmod(p, 0o644)
    return str(p), hashlib.sha256(data).hexdigest()


def _relaxed(monkeypatch) -> None:
    monkeypatch.setattr(apply_tool, "_PG_REQUIRE_ROOT_OWNED", False)
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)


def _open(path: str, sha: str | None, runtime_files, *, major: str = "18"):
    return apply_tool.open_verified_pg_restore(
        expected_sha256=sha, expected_major=major, runtime_files=runtime_files, path=path)


# --------------------------------------------------------------------------- #
# happy path (relaxed ownership) + basic rejections
# --------------------------------------------------------------------------- #
def test_valid_returns_a_held_context(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    vpr = _open(path, sha, ((path, sha),))
    assert vpr is not None and vpr.major == "18"
    r = vpr.run(["--version"], timeout=30)
    assert r is not None and r.returncode == 0 and "PostgreSQL) 18" in r.stdout
    vpr.close()


def test_absent_path_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    assert _open(str(tmp_path / "nope"), "d" * 64, ((str(tmp_path / "nope"), "d" * 64),)) is None


def test_none_and_malformed_sha_return_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    assert _open(path, None, ((path, sha),)) is None
    assert _open(path, "not-a-sha", ((path, sha),)) is None


def test_sha_mismatch_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, _sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    assert _open(path, "d" * 64, ((path, "d" * 64),)) is None


def test_wrong_major_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    for major in ("17", "19"):
        path, sha = _write_exec(tmp_path / f"pg_restore_{major}", _fake_body(major=major))
        assert _open(path, sha, ((path, sha),)) is None


def test_group_or_world_writable_binary_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    for mode in (0o775, 0o757):
        p = tmp_path / f"pgr_{mode:o}"
        path, sha = _write_exec(p, _fake_body(), mode=mode)
        assert _open(path, sha, ((path, sha),)) is None


def test_final_symlink_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "real_pg_restore", _fake_body())
    link = tmp_path / "pg_restore"
    os.symlink(path, link)  # O_NOFOLLOW on the final component must refuse
    assert _open(str(link), sha, ((path, sha),)) is None


def test_parent_symlink_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    realdir = tmp_path / "realdir"
    realdir.mkdir()
    path, sha = _write_exec(realdir / "pg_restore", _fake_body())
    linkdir = tmp_path / "linkdir"
    os.symlink(realdir, linkdir)  # a symlinked parent component must be rejected in traversal
    assert _open(str(linkdir / "pg_restore"), sha, ((path, sha),)) is None


def test_empty_manifest_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    assert _open(path, sha, ()) is None  # no documented library manifest -> fail closed


def test_library_tamper_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    lib, _libsha = _libfile(tmp_path)
    assert _open(path, sha, ((lib, "d" * 64),)) is None  # documented lib sha != on-disk


def test_absent_library_tolerated_only_when_relaxed(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    # a documented library that is absent locally is tolerated in the relaxed (non-cloud) test mode
    vpr = _open(path, sha, (("/usr/lib/x86_64-linux-gnu/libpq.so.5.18", "c" * 64),))
    assert vpr is not None
    vpr.close()


@root_only
def test_non_root_owned_binary_returns_none(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)  # relaxed accepts the euid (0), but NOT an arbitrary other uid
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    os.chown(path, 65534, 65534)  # nobody -> neither root nor euid
    assert _open(path, sha, ((path, sha),)) is None


# --------------------------------------------------------------------------- #
# override policy (§6): cloud ignores CESTAPLAN_PG_RESTORE_PATH; self_hosted honors it
# --------------------------------------------------------------------------- #
def test_cloud_ignores_override_and_blocks(monkeypatch, tmp_path: Path) -> None:
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("CESTAPLAN_PG_RESTORE_PATH", path)
    assert apply_tool._pg_restore_operational_path() == apply_tool.PG_RESTORE_DEFAULT_PATH
    # cloud also forces the strict gates, so the absent /usr/lib default fails closed
    assert _open(apply_tool._pg_restore_operational_path(), sha, ((path, sha),)) is None


def test_self_hosted_honors_override(monkeypatch, tmp_path: Path) -> None:
    path, _sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    monkeypatch.setenv("CESTAPLAN_PG_RESTORE_PATH", path)
    assert apply_tool._pg_restore_operational_path() == path


def test_strict_mode_rejects_world_writable_ancestor(monkeypatch, tmp_path: Path) -> None:
    # default (strict) mode: /tmp is world-writable, so the ancestor check fails closed even for a
    # perfectly-formed fixture binary. (The strict happy path is covered by CI's image-runtime job.)
    monkeypatch.setattr(apply_tool, "_PG_REQUIRE_ROOT_OWNED", True)
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    assert _open(path, sha, ((path, sha),)) is None


# --------------------------------------------------------------------------- #
# TOCTOU (§7): the executable actually run is the held fd, never a re-resolved path
# --------------------------------------------------------------------------- #
def test_path_substitution_after_verify_never_runs_the_new_binary(monkeypatch,
                                                                  tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    lib, libsha = _libfile(tmp_path)  # a SEPARATE, stable manifest entry (untouched by the swap)
    vpr = _open(path, sha, ((lib, libsha),))
    assert vpr is not None
    # swap a MALICIOUS binary into the same path AFTER verification. os.replace unlinks the original
    # inode the fd still holds, changing its ctime -> the held-fd identity check blocks the run. The
    # malicious binary (major 99 / EVIL) is NEVER executed, whether by re-resolution or otherwise.
    mal = tmp_path / "mal"
    _write_exec(mal, _fake_body(major="99", marker=" EVIL"))
    os.replace(str(mal), path)
    r = vpr.run(["--version"], timeout=30)
    assert r is None or ("PostgreSQL) 18" in r.stdout and "EVIL" not in r.stdout)
    vpr.close()


def test_parent_rename_after_verify_keeps_original_executable(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    d = tmp_path / "bin"
    d.mkdir()
    path, sha = _write_exec(d / "pg_restore", _fake_body())
    lib, libsha = _libfile(tmp_path)
    vpr = _open(path, sha, ((lib, libsha),))
    assert vpr is not None
    os.rename(str(d), str(tmp_path / "bin_moved"))  # rename the parent AFTER verification
    r = vpr.run(["--version"], timeout=30)
    assert r is not None and "PostgreSQL) 18" in r.stdout  # still the held fd
    vpr.close()


def test_inplace_modification_blocks_execution(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    vpr = _open(path, sha, ((path, sha),))
    assert vpr is not None
    with open(path, "r+b") as f:  # modify the SAME inode the fd holds
        f.seek(0)
        f.write(b"#!/bin/sh\necho tampered\n")
    assert vpr.run(["--version"], timeout=30) is None  # held-fd sha changed -> blocked
    vpr.close()


def test_truncation_blocks_execution(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    path, sha = _write_exec(tmp_path / "pg_restore", _fake_body())
    vpr = _open(path, sha, ((path, sha),))
    assert vpr is not None
    os.truncate(path, 5)
    assert vpr.run(["--version"], timeout=30) is None
    vpr.close()


def test_failure_never_raises_or_leaks_a_path(monkeypatch, tmp_path: Path) -> None:
    _relaxed(monkeypatch)
    secret = tmp_path / "super-secret-path-pg_restore"
    # every failure mode returns None (never an exception carrying the path)
    assert _open(str(secret), "d" * 64, ((str(secret), "d" * 64),)) is None


# --------------------------------------------------------------------------- #
# operational path default
# --------------------------------------------------------------------------- #
def test_default_operational_path_is_the_pinned_binary(monkeypatch) -> None:
    monkeypatch.delenv("CESTAPLAN_PG_RESTORE_PATH", raising=False)
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert apply_tool._pg_restore_operational_path() == "/usr/lib/postgresql/18/bin/pg_restore"
    assert apply_tool.PG_RESTORE_REQUIRED_MAJOR == "18"
