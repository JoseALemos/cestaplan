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
    assert _code(e) == "o_nofollow_unavailable"


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
