"""Sealed authorization-package loader tests (feat immutable-build-provenance).

Uses EPHEMERAL Ed25519 keys generated per-test — never a real production key. Covers signature
verification, unauthorized keys, tamper detection, schema/field/expiry/plan_hash validation,
sensitive-data rejection, python -O, and that the CLI keeps blocking apply/restore/simulate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cestaplan_api.provenance.authorization import (
    AuthorizationError,
    load_authorization_package,
)

_PLAN = "d" * 64
_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _pubkey_hex(sk: Ed25519PrivateKey) -> str:
    return sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _base_package(**over) -> dict:
    pkg = {
        "schema_version": 1,
        "authorization_id": "auth-2026-07-27-001",
        "plan_hash": _PLAN,
        "main_commit_sha": "e" * 40,
        "alembic_revision": "360a55cb6abb",
        "expected_commit_sha": "e" * 40,
        "expected_source_hash": "1" * 64,
        "expected_api_artifact_hash": "2" * 64,
        "expected_worker_artifact_hash": "3" * 64,
        "expected_document_hash": "4" * 64,
        "expected_product_price": 0,
        "expected_active_mappings": 0,
        "generated_at": (_NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
        "operator_reference": "ops/ticket-OPS-42",
        "backup_expected_sha256": "5" * 64,
        "backup_storage_reference": "s3://cestaplan-backups/apply/backup.dump",
    }
    pkg.update(over)
    return pkg


def _canonical_bytes(pkg: dict) -> bytes:
    """The one valid encoding: canonical JSON (with self-hash) + trailing newline."""
    return (_canonical(pkg) + "\n").encode()


def _seal(pkg: dict, *, recompute_hash: bool = True) -> bytes:
    pkg = dict(pkg)
    if recompute_hash:
        pkg["authorization_package_hash"] = hashlib.sha256(_canonical(pkg).encode()).hexdigest()
    return _canonical_bytes(pkg)


def _sign(package_bytes: bytes, sk: Ed25519PrivateKey) -> str:
    return sk.sign(package_bytes).hex()


def _make(*, sk: Ed25519PrivateKey | None = None, recompute_hash: bool = True, **over):
    sk = sk or Ed25519PrivateKey.generate()
    body = _seal(_base_package(**over), recompute_hash=recompute_hash)
    return body, _sign(body, sk), _pubkey_hex(sk)


def test_valid_package_loads(tmp_path) -> None:
    body, sig, pk = _make()
    pkg = load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                     expected_plan_hash=_PLAN)
    assert pkg.plan_hash == _PLAN and pkg.main_commit_sha == "e" * 40
    assert pkg.expected_provenance_fields()["source_tree_hash"] == "1" * 64
    assert pkg.expected_product_price == 0 and pkg.expected_active_mappings == 0
    assert len(pkg.public_key_fingerprint) == 16


def test_unauthorized_key_is_rejected() -> None:
    body, sig, _pk = _make()
    other = _pubkey_hex(Ed25519PrivateKey.generate())
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[other], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "signature_not_authorized"


def test_no_authorized_keys_is_rejected() -> None:
    body, sig, _pk = _make()
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "no_authorized_public_keys"


def test_malformed_signature_is_rejected() -> None:
    body, _sig, pk = _make()
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, "ab", authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "signature_malformed"


def test_tampered_body_breaks_signature() -> None:
    sk = Ed25519PrivateKey.generate()
    body, sig, pk = _make(sk=sk)
    tampered = body.replace(b"e" * 40, b"f" * 40)  # change after signing
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(tampered, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "signature_not_authorized"


def test_altered_package_with_stale_self_hash_is_rejected() -> None:
    sk = Ed25519PrivateKey.generate()
    pkg = _base_package()
    pkg["authorization_package_hash"] = hashlib.sha256(_canonical(pkg).encode()).hexdigest()
    pkg["expected_product_price"] = 99  # mutate AFTER computing the self-hash
    body = _canonical_bytes(pkg)  # canonical bytes of the mutated package (stale self-hash)
    sig = _sign(body, sk)  # re-sign so the signature is valid over the altered bytes
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "authorization_package_hash_mismatch"


def test_expired_package_is_rejected() -> None:
    body, sig, pk = _make(generated_at=(_NOW - timedelta(hours=3)).isoformat(),
                          expires_at=(_NOW - timedelta(hours=1)).isoformat())
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_expired"


def test_lifetime_too_long_is_rejected() -> None:
    body, sig, pk = _make(generated_at=(_NOW - timedelta(minutes=1)).isoformat(),
                          expires_at=(_NOW + timedelta(days=7)).isoformat())
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "lifetime_too_long"


def test_plan_hash_mismatch_is_rejected() -> None:
    body, sig, pk = _make()
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash="0" * 64)
    assert ei.value.code == "plan_hash_mismatch"


def test_alembic_revision_invalid_is_rejected() -> None:
    body, sig, pk = _make(alembic_revision="NOT VALID!")
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "alembic_revision_invalid"


def test_malformed_commit_is_rejected() -> None:
    body, sig, pk = _make(main_commit_sha="z" * 40)
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "main_commit_sha_invalid"


def test_unknown_field_is_rejected() -> None:
    body, sig, pk = _make(surprise="x")
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_fields_mismatch"


def test_missing_field_is_rejected() -> None:
    sk = Ed25519PrivateKey.generate()
    pkg = _base_package()
    del pkg["expected_source_hash"]
    pkg["authorization_package_hash"] = hashlib.sha256(_canonical(pkg).encode()).hexdigest()
    body = _canonical_bytes(pkg)
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, _sign(body, sk),
                                   authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_fields_mismatch"


# ---- v2: strictly-canonical package, RFC3339/tz, race-safe file loading ----
def test_non_canonical_bytes_rejected() -> None:  # §4
    sk = Ed25519PrivateKey.generate()
    pkg = _base_package()
    pkg["authorization_package_hash"] = hashlib.sha256(_canonical(pkg).encode()).hexdigest()
    body = (json.dumps(pkg, indent=2) + "\n").encode()  # valid JSON but NOT canonical (whitespace)
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, _sign(body, sk),
                                   authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_not_canonical"


def test_missing_trailing_newline_rejected() -> None:  # §4
    sk = Ed25519PrivateKey.generate()
    body = _seal(_base_package()).rstrip(b"\n")  # drop the required trailing newline
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, _sign(body, sk),
                                   authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_not_canonical"


def test_duplicate_json_key_rejected() -> None:  # §4
    sk = Ed25519PrivateKey.generate()
    body = _seal(_base_package())
    injected = body.replace(b'{', b'{"schema_version":1,', 1)  # a duplicate schema_version key
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(injected, _sign(injected, sk),
                                   authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "duplicate_json_key"


def test_uppercase_signature_rejected() -> None:  # §4
    body, sig, pk = _make()
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig.upper(), authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "signature_malformed"


def test_naive_now_rejected() -> None:  # §4
    body, sig, pk = _make()
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk],
                                   now=datetime(2026, 7, 27, 12, 0), expected_plan_hash=_PLAN)
    assert ei.value.code == "now_not_tz_aware"


def test_non_rfc3339_timestamp_rejected() -> None:  # §4
    body, sig, pk = _make(generated_at="2026-07-27 11:55:00+00:00")  # space, not 'T'
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "generated_at_invalid"


def test_non_utc_timestamp_rejected() -> None:  # §4
    body, sig, pk = _make(generated_at="2026-07-27T13:55:00+02:00")  # not UTC
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "generated_at_invalid"


def test_generated_too_long_ago_rejected() -> None:  # §4
    body, sig, pk = _make(generated_at=(_NOW - timedelta(hours=2)).isoformat(),
                          expires_at=(_NOW + timedelta(hours=2)).isoformat())  # unexpired but stale
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "package_generated_too_long_ago"


def test_z_suffix_timestamp_accepted() -> None:  # §4 — RFC3339 'Z' is canonical UTC
    body, sig, pk = _make(generated_at="2026-07-27T11:55:00Z", expires_at="2026-07-27T12:30:00Z")
    pkg = load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                     expected_plan_hash=_PLAN)
    assert pkg.generated_at.tzinfo is not None


def test_file_loader_bad_utf8_rejected(tmp_path) -> None:  # §4
    from cestaplan_api.provenance import load_authorization_package_from_files
    sk = Ed25519PrivateKey.generate()
    body = b"\xff\xfe not valid utf-8"
    pkg_path = tmp_path / "pkg.json"
    sig_path = tmp_path / "pkg.sig"
    pkg_path.write_bytes(body)
    sig_path.write_text(sk.sign(body).hex())  # valid signature over the (bad-utf8) bytes
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package_from_files(pkg_path, sig_path,
                                              authorized_public_keys=[_pubkey_hex(sk)], now=_NOW,
                                              expected_plan_hash=_PLAN)
    assert ei.value.code == "package_not_utf8"


def test_file_loader_missing_file_sanitized(tmp_path) -> None:  # §4 — no path/traceback leak
    from cestaplan_api.provenance import load_authorization_package_from_files
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package_from_files(tmp_path / "nope.json", tmp_path / "nope.sig",
                                              authorized_public_keys=[], now=_NOW,
                                              expected_plan_hash=_PLAN)
    assert ei.value.code == "package_unreadable" and str(tmp_path) not in str(ei.value)


def test_sensitive_data_is_rejected() -> None:
    body, sig, pk = _make(authorization_id="tokenXYZ", operator_reference="password=hunter2")
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code == "sensitive_data_in_package"


def test_unsanitized_storage_reference_is_rejected() -> None:
    body, sig, pk = _make(backup_storage_reference="/var/lib/postgresql/backup.dump")
    with pytest.raises(AuthorizationError) as ei:
        load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                   expected_plan_hash=_PLAN)
    assert ei.value.code in ("backup_storage_reference_unsanitized", "sensitive_data_in_package")


def test_package_feeds_expected_provenance_gate() -> None:
    # A valid package feeds ExpectedProvenance; an observed build that differs must NOT match.
    from cestaplan_api.tools import apply_history_lane_remediation as A
    body, sig, pk = _make()
    pkg = load_authorization_package(body, sig, authorized_public_keys=[pk], now=_NOW,
                                     expected_plan_hash=_PLAN)
    expected = A.ExpectedProvenance(**pkg.expected_provenance_fields())
    observed = A.BuildProvenance(commit_sha="e" * 40, source_tree_hash="9" * 64,
                                 api_artifact_hash="2" * 64, worker_artifact_hash="3" * 64,
                                 document_hash="4" * 64)  # source hash differs from expected
    ctx = A.ApplyContext(app_commit_sha="e" * 40, deployed_api_sha="e" * 40,
                         deployed_worker_sha="e" * 40, expected_main_sha="e" * 40,
                         observed_provenance=observed, expected_provenance=expected)
    gates = dict(A._provenance_gates({"commit_provenance": {}}, ctx))
    assert gates["provenance_source_matches"] is False
    assert gates["immutable_build_provenance"] is False


def test_cli_blocks_apply_restore_simulate_in_cloud(monkeypatch) -> None:
    from cestaplan_api.tools import apply_history_lane_remediation as A
    monkeypatch.setenv("DEPLOYMENT_MODE", "production")
    monkeypatch.setattr(A, "load_manifest", lambda _p: {"plan_hash": _PLAN})
    for argv in (["--apply", "--manifest-path", "x"], ["--restore", "r", "--manifest-path", "x"],
                 ["--simulate", "--manifest-path", "x"]):
        with pytest.raises(SystemExit) as ei:
            A.main(argv)
        assert "ABORT" in str(ei.value)


def test_loader_holds_under_optimize() -> None:
    code = (
        "from cestaplan_api.provenance.authorization import load_authorization_package as L, "
        "AuthorizationError as E\n"
        "try:\n"
        "    L(b'{}', 'ab', authorized_public_keys=[], now=__import__('datetime').datetime.now("
        "__import__('datetime').timezone.utc), expected_plan_hash='d'*64); print('NO_RAISE')\n"
        "except E as e:\n"
        "    print('RAISED', e.code)\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert "RAISED no_authorized_public_keys" in out.stdout
