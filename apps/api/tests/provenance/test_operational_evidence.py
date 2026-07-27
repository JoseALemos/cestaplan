"""OperationalCeremonyEvidenceV1 loader tests (verify-only ceremony adapter).

The evidence is OBSERVED input only; it is read fail-closed (regular file, O_NOFOLLOW, owner-only
perms, race-safe) and the local backup path is never rendered. No private key/signing anywhere."""

from __future__ import annotations

import json
import os
import stat as statmod
from pathlib import Path

import pytest

from cestaplan_api.provenance import operational_evidence as oe
from cestaplan_api.provenance.operational_evidence import (
    CeremonyFileError,
    load_operational_evidence,
    secure_read_bytes,
)

_VALID = {
    "schema_version": 1,
    "deployed_api_sha": "a" * 40,
    "deployed_worker_sha": "a" * 40,
    "backup": {
        "path": "/var/backups/apply.dump",
        "expected_sha256": "b" * 64,
        "created_at": "2026-07-27T12:00:00+00:00",
        "expected_postgres_version": "18",
        "storage_reference": "s3://cestaplan-backups/apply.dump",
    },
}


def _canon(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def _write(tmp_path: Path, doc: dict, *, mode: int = 0o600, name: str = "evidence.json") -> str:
    p = tmp_path / name
    p.write_text(_canon(doc), encoding="utf-8")
    os.chmod(p, mode)
    return str(p)


def _write_raw(tmp_path: Path, raw: str, *, mode: int = 0o600) -> str:
    p = tmp_path / "evidence.json"
    p.write_text(raw, encoding="utf-8")
    os.chmod(p, mode)
    return str(p)


def _code(exc: pytest.ExceptionInfo) -> str:
    return exc.value.code  # type: ignore[attr-defined]


def test_valid_document_loads(tmp_path: Path) -> None:
    ev = load_operational_evidence(_write(tmp_path, _VALID))
    assert ev.deployed_api_sha == "a" * 40
    assert ev.deployed_worker_sha == "a" * 40
    assert ev.backup_path == "/var/backups/apply.dump"
    assert ev.backup_expected_sha256 == "b" * 64
    assert ev.backup_expected_postgres_version == "18"
    assert ev.backup_storage_reference == "s3://cestaplan-backups/apply.dump"
    assert ev.backup_created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_unknown_top_field_rejected(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, {**_VALID, "extra": 1}))
    assert _code(e) == "fields_mismatch"


def test_missing_top_field_rejected(tmp_path: Path) -> None:
    d = {k: v for k, v in _VALID.items() if k != "deployed_worker_sha"}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "fields_mismatch"


def test_unknown_backup_field_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "surprise": 1}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_fields_mismatch"


def test_invalid_sha_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "expected_sha256": "XY" + "b" * 62}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_expected_sha256_invalid"


def test_invalid_commit_rejected(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, {**_VALID, "deployed_api_sha": "z" * 40}))
    assert _code(e) == "deployed_api_sha_invalid"


def test_non_utc_timestamp_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "created_at": "2026-07-27T12:00:00+02:00"}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "created_at_invalid"


def test_relative_path_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "path": "relative/apply.dump"}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_path_invalid"


def test_control_char_in_path_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "path": "/var/backups/apply.dump"}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_path_invalid"


def test_symlink_evidence_rejected(tmp_path: Path) -> None:
    real = _write(tmp_path, _VALID, name="real.json")
    link = tmp_path / "link.json"
    os.symlink(real, link)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(str(link))
    assert _code(e) == "symlink_rejected"


def test_world_readable_evidence_rejected(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, _VALID, mode=0o644))
    assert _code(e) == "permissions_too_open"


def test_group_writable_evidence_rejected(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, _VALID, mode=0o620))
    assert _code(e) == "permissions_too_open"


def test_change_during_read_rejected(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, _VALID)
    real_read = os.read
    state = {"done": False}

    def racing_read(fd, n):
        data = real_read(fd, n)
        if data and not state["done"]:
            state["done"] = True
            with open(path, "w", encoding="utf-8") as f:
                f.write("{}")  # truncate to a different size mid-read
        return data

    monkeypatch.setattr(oe.os, "read", racing_read)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(path)
    assert _code(e) == "file_changed_during_read"


def test_invalid_storage_reference_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "storage_reference": "../etc/passwd"}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_storage_reference_invalid"


def test_invalid_pg_version_rejected(tmp_path: Path) -> None:
    d = {**_VALID, "backup": {**_VALID["backup"], "expected_postgres_version": "eighteen"}}
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write(tmp_path, d))
    assert _code(e) == "backup_expected_postgres_version_invalid"


def test_duplicate_keys_rejected(tmp_path: Path) -> None:
    raw = ('{"schema_version":1,"schema_version":1,"deployed_api_sha":"' + "a" * 40
           + '","deployed_worker_sha":"' + "a" * 40 + '","backup":{}}\n')
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, raw))
    assert _code(e) == "duplicate_key"


def test_non_finite_number_rejected(tmp_path: Path) -> None:
    b = _VALID["backup"]
    raw = ('{"schema_version":NaN,"deployed_api_sha":"' + "a" * 40 + '","deployed_worker_sha":"'
           + "a" * 40 + '","backup":' + json.dumps(b) + "}\n")
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, raw))
    assert _code(e) == "non_finite_number"


def test_backup_path_never_in_repr(tmp_path: Path) -> None:
    ev = load_operational_evidence(_write(tmp_path, _VALID))
    assert "/var/backups/apply.dump" not in repr(ev)  # path is repr=False (never leaked)


def test_o_nofollow_unavailable_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, _VALID)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(path)
    assert _code(e) == "open_primitive_unavailable"  # fail-closed on any missing open primitive


def test_secure_read_rejects_irregular_file(tmp_path: Path) -> None:
    # a directory is not a regular file
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(tmp_path), require_owner_only=False)
    assert _code(e) in ("not_regular_file", "file_unreadable")


def test_secure_read_owner_only_ok(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"hello")
    os.chmod(p, 0o600)
    assert secure_read_bytes(str(p), require_owner_only=True) == b"hello"
    assert statmod.S_ISREG(os.stat(p).st_mode)


# --------------------------------------------------------------------------- #
# §3v2: strict single canonical encoding
# --------------------------------------------------------------------------- #
def test_indented_json_rejected(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    p.write_text(json.dumps(_VALID, indent=2) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(str(p))
    assert _code(e) == "not_canonical"


def test_extra_whitespace_rejected(tmp_path: Path) -> None:
    raw = json.dumps(_VALID, sort_keys=True, separators=(", ", ": ")) + "\n"
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, raw))
    assert _code(e) == "not_canonical"


def test_key_order_rejected(tmp_path: Path) -> None:
    raw = json.dumps(_VALID, sort_keys=False, separators=(",", ":")) + "\n"
    # force a non-sorted top-level order
    raw = ('{"backup":' + json.dumps(_VALID["backup"], sort_keys=True, separators=(",", ":"))
           + ',"schema_version":1,"deployed_api_sha":"' + "a" * 40 + '","deployed_worker_sha":"'
           + "a" * 40 + '"}\n')
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, raw))
    assert _code(e) == "not_canonical"


def test_missing_newline_rejected(tmp_path: Path) -> None:
    raw = json.dumps(_VALID, sort_keys=True, separators=(",", ":"))  # canonical, NO trailing NL
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, raw))
    assert _code(e) == "not_canonical"


def test_double_newline_rejected(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(_write_raw(tmp_path, _canon(_VALID) + "\n\n"))
    assert _code(e) == "not_canonical"


def test_crlf_rejected(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    p.write_bytes((_canon(_VALID) + "\n").encode().replace(b"\n", b"\r\n"))
    os.chmod(p, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(str(p))
    assert _code(e) == "not_canonical"


def test_bom_rejected(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    p.write_bytes("﻿".encode() + (_canon(_VALID) + "\n").encode())
    os.chmod(p, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(str(p))
    assert _code(e) == "not_canonical"


def test_file_too_large_rejected(tmp_path: Path) -> None:
    d = dict(_VALID)
    d["backup"] = {**_VALID["backup"], "storage_reference": "s3://b/" + "a" * 70000}
    p = tmp_path / "e.json"
    p.write_text(_canon(d) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        load_operational_evidence(str(p))
    assert _code(e) == "file_too_large"


# --------------------------------------------------------------------------- #
# §4v2: secure_read_bytes race-safety + traversal
# --------------------------------------------------------------------------- #
def test_secure_read_relative_rejected() -> None:
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes("relative/path.json")
    assert _code(e) == "path_not_absolute"


def test_secure_read_parent_component_symlink_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_bytes(b"x")
    link = tmp_path / "link"
    os.symlink(real, link, target_is_directory=True)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(link / "f"))
    assert _code(e) == "symlink_rejected"


def test_secure_read_final_symlink_rejected(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_bytes(b"x")
    link = tmp_path / "l"
    os.symlink(target, link)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(link))
    assert _code(e) == "symlink_rejected"


def test_secure_read_change_during_read_rejected(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"A" * 50)
    real_read = os.read
    state = {"done": False}

    def racing(fd, n):
        d = real_read(fd, n)
        if d and not state["done"]:
            state["done"] = True
            with open(p, "wb") as f:
                f.write(b"B" * 50)  # same size, different content + new mtime
        return d

    monkeypatch.setattr(oe.os, "read", racing)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(p))
    assert _code(e) == "file_changed_during_read"


def test_secure_read_atomic_replace_rejected(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"A" * 50)
    other = tmp_path / "o"
    other.write_bytes(b"C" * 50)
    real_read = os.read
    state = {"done": False}

    def racing(fd, n):
        d = real_read(fd, n)
        if d and not state["done"]:
            state["done"] = True
            os.replace(str(other), str(p))  # new inode over the same path
        return d

    monkeypatch.setattr(oe.os, "read", racing)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(p))
    assert _code(e) == "file_changed_during_read"


def test_secure_read_size_cap(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"x" * 100)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(p), max_bytes=10)
    assert _code(e) == "file_too_large"


def test_secure_read_stable_passes(tmp_path: Path) -> None:
    p = tmp_path / "f"
    p.write_bytes(b"hello world")
    assert secure_read_bytes(str(p)) == b"hello world"


# --------------------------------------------------------------------------- #
# §1v2: secure_create_request_file
# --------------------------------------------------------------------------- #
from cestaplan_api.provenance.operational_evidence import secure_create_request_file  # noqa: E402

_PAYLOAD = b'{"request_schema_version":1}\n'


def test_create_valid_new_output(tmp_path: Path) -> None:
    out = tmp_path / "req.json"
    secure_create_request_file(str(out), _PAYLOAD)
    assert out.read_bytes() == _PAYLOAD
    assert (os.stat(out).st_mode & 0o777) == 0o600  # exactly 0600


def test_create_existing_output_blocks_unmodified(tmp_path: Path) -> None:
    out = tmp_path / "req.json"
    out.write_bytes(b"ORIGINAL")
    os.chmod(out, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(out), _PAYLOAD)
    assert _code(e) == "output_exists"
    assert out.read_bytes() == b"ORIGINAL"  # never modified


def test_create_symlink_output_blocks(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_bytes(b"KEEP")
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(CeremonyFileError):
        secure_create_request_file(str(link), _PAYLOAD)
    assert target.read_bytes() == b"KEEP"  # target untouched


def test_create_parent_symlink_blocks(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linkdir"
    os.symlink(real, link, target_is_directory=True)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(link / "req.json"), _PAYLOAD)
    assert _code(e) == "symlink_rejected"
    assert not (real / "req.json").exists()  # nothing created


def test_create_relative_blocks(tmp_path: Path) -> None:
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file("relative/req.json", _PAYLOAD)
    assert _code(e) == "path_not_absolute"


def test_create_inside_repo_blocks() -> None:
    repo_root = str(Path(__file__).resolve().parents[3])  # .../cestaplan
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(os.path.join(repo_root, "should_not_exist.json"), _PAYLOAD)
    assert _code(e) == "output_inside_repo"
    assert not os.path.exists(os.path.join(repo_root, "should_not_exist.json"))


def test_create_inside_app_blocks() -> None:
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file("/app/should_not_exist.json", _PAYLOAD)
    assert _code(e) == "output_inside_app"


def test_create_equal_to_backup_blocks_and_backup_intact(tmp_path: Path) -> None:
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"BACKUP-BYTES")
    os.chmod(backup, 0o600)
    before = backup.read_bytes()
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(backup), _PAYLOAD)
    assert _code(e) == "output_exists"
    assert backup.read_bytes() == before  # byte-identical


def test_create_write_failure_leaves_no_partial(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "req.json"

    def boom(_fd, _buf):
        raise OSError("disk full")

    monkeypatch.setattr(oe.os, "write", boom)
    with pytest.raises(CeremonyFileError):
        secure_create_request_file(str(out), _PAYLOAD)
    assert not out.exists()  # the partially-created file was unlinked


def test_create_errors_never_contain_path(tmp_path: Path) -> None:
    out = tmp_path / "secretname.json"
    out.write_bytes(b"x")
    os.chmod(out, 0o600)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(out), _PAYLOAD)
    assert "secretname" not in str(e.value) and "secretname" not in e.value.code


# --------------------------------------------------------------------------- #
# §1v3: adversarial races in traversal + second read
# --------------------------------------------------------------------------- #
def test_traversal_dir_substituted_between_lstat_and_open(tmp_path: Path, monkeypatch) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_bytes(b"x")
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "f").write_bytes(b"y")
    real_open = oe.os.open
    state = {"done": False}

    def racing_open(name, flags, *a, **k):
        if not state["done"] and name == "d":
            state["done"] = True
            os.replace(str(d2), str(d))  # swap the dir (new inode) between lstat and open
        return real_open(name, flags, *a, **k)

    monkeypatch.setattr(oe.os, "open", racing_open)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(d / "f"))
    assert _code(e) in ("directory_changed_during_traversal", "path_unreadable")


def _read_race(tmp_path, mutate, monkeypatch):
    p = tmp_path / "f"
    p.write_bytes(b"A" * 40)
    real_read = oe.os.read
    reads = {"n": 0}

    def read_hook(fd, n):
        d = real_read(fd, n)
        if d:
            reads["n"] += 1
            if reads["n"] == 2:  # during the SECOND read (after second_before fstat)
                mutate(p)
        return d

    monkeypatch.setattr(oe.os, "read", read_hook)
    with pytest.raises(CeremonyFileError) as e:
        secure_read_bytes(str(p))
    assert _code(e) == "file_changed_during_read"


def test_secure_read_change_during_second_read(tmp_path: Path, monkeypatch) -> None:
    _read_race(tmp_path, lambda p: p.write_bytes(b"B" * 80), monkeypatch)  # different size


def test_secure_read_change_and_restore_bytes_second_read(tmp_path: Path, monkeypatch) -> None:
    def mutate(p):
        p.write_bytes(b"A" * 40)  # same bytes, but a new mtime
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    _read_race(tmp_path, mutate, monkeypatch)


def test_secure_read_metadata_change_no_size(tmp_path: Path, monkeypatch) -> None:
    _read_race(tmp_path, lambda p: os.chmod(p, 0o640), monkeypatch)  # mode/ctime change, same size


# --------------------------------------------------------------------------- #
# §3v3: output directory policy + durability + cleanup
# --------------------------------------------------------------------------- #
def test_create_final_dir_group_writable_blocks(tmp_path: Path) -> None:
    d = tmp_path / "grp"
    d.mkdir()
    os.chmod(d, 0o770)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(d / "req.json"), _PAYLOAD)
    assert _code(e) == "output_directory_insecure"


def test_create_final_dir_world_writable_blocks(tmp_path: Path) -> None:
    d = tmp_path / "wrld"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(d / "req.json"), _PAYLOAD)
    assert _code(e) == "output_directory_insecure"


def test_create_private_0700_dir_passes(tmp_path: Path) -> None:
    d = tmp_path / "priv"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    out = d / "req.json"
    secure_create_request_file(str(out), _PAYLOAD)
    assert out.read_bytes() == _PAYLOAD and (os.stat(out).st_mode & 0o777) == 0o600


def test_create_non_sticky_world_writable_ancestor_blocks(tmp_path: Path) -> None:
    anc = tmp_path / "anc"
    anc.mkdir()
    os.chmod(anc, 0o777)  # world-writable, NO sticky bit
    sub = anc / "sub"
    sub.mkdir(mode=0o700)
    os.chmod(sub, 0o700)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(sub / "req.json"), _PAYLOAD)
    assert _code(e) == "output_directory_insecure"


def test_create_fsync_file_failure_no_partial(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "req.json"
    real = oe.os.fsync
    calls = {"n": 0}

    def fsync_hook(fd):
        calls["n"] += 1
        if calls["n"] == 1:  # the file fsync
            raise OSError("no fsync")
        return real(fd)

    monkeypatch.setattr(oe.os, "fsync", fsync_hook)
    with pytest.raises(CeremonyFileError):
        secure_create_request_file(str(out), _PAYLOAD)
    assert not out.exists()  # partial cleaned up


def test_create_parent_fsync_failure_fails_closed(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "req.json"
    real = oe.os.fsync
    calls = {"n": 0}

    def fsync_hook(fd):
        calls["n"] += 1
        if calls["n"] == 2:  # the parent-dir fsync
            raise OSError("no dir fsync")
        return real(fd)

    monkeypatch.setattr(oe.os, "fsync", fsync_hook)
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(out), _PAYLOAD)
    assert _code(e) == "parent_fsync_failed"
    assert not out.exists()  # our file cleaned up


def test_create_cleanup_failure_returns_specific_code(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "req.json"
    monkeypatch.setattr(oe.os, "fsync",
                        lambda fd: (_ for _ in ()).throw(OSError("boom")))  # force error path
    monkeypatch.setattr(oe.os, "unlink",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("cannot unlink")))
    with pytest.raises(CeremonyFileError) as e:
        secure_create_request_file(str(out), _PAYLOAD)
    assert _code(e) == "output_cleanup_failed"


def test_create_substitution_before_cleanup_never_deletes_substitute(tmp_path: Path,
                                                                     monkeypatch) -> None:
    out = tmp_path / "req.json"
    substitute = tmp_path / "sub.dat"
    substitute.write_bytes(b"SUBSTITUTE")
    real = oe.os.fsync
    calls = {"n": 0}

    def fsync_hook(fd):
        calls["n"] += 1
        if calls["n"] == 2:  # right before cleanup: swap our file for a different inode
            os.replace(str(substitute), str(out))
            raise OSError("parent fsync failed")
        return real(fd)

    monkeypatch.setattr(oe.os, "fsync", fsync_hook)
    with pytest.raises(CeremonyFileError):
        secure_create_request_file(str(out), _PAYLOAD)
    assert out.read_bytes() == b"SUBSTITUTE"  # the substitute was NOT deleted
