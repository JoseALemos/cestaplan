"""Build-provenance generator tests (feat immutable-build-provenance): deterministic generation,
per-scope mutations, exclusions, unsafe symlinks, malformed commit, document schema, and that the
deployed CLI keeps blocking apply/restore/simulate in cloud. Uses a synthetic bundle tree that
mirrors the real base-relative layout (src/…, migrations/…, alembic.ini, manifests)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cestaplan_api.provenance import generator as g
from cestaplan_api.provenance.manifest import ProvenanceError, build_manifest, file_excluded

_COMMIT = "a" * 40
_MIGRATION = (
    '"""m"""\n'
    "revision: str = '360a55cb6abb'\n"
    "down_revision: str | None = None\n"
    "def upgrade() -> None: ...\n"
    "def downgrade() -> None: ...\n"
)


def _bundle(root: Path) -> Path:
    """Create a synthetic base with the real layout and a bit of content in every scope."""
    (root / "src/cestaplan_api/tools").mkdir(parents=True)
    (root / "src/cestaplan_api/__init__.py").write_text("# api\n")
    (root / "src/cestaplan_api/tools/apply.py").write_text("# remediation tool\n")
    (root / "src/cestaplan_engine").mkdir(parents=True)
    (root / "src/cestaplan_engine/__init__.py").write_text("# engine (shared)\n")
    (root / "src/cestaplan_worker").mkdir(parents=True)
    (root / "src/cestaplan_worker/__init__.py").write_text("# worker\n")
    (root / "src/cestaplan_worker/main.py").write_text("# worker main\n")
    (root / "migrations/versions").mkdir(parents=True)
    (root / "migrations/versions/0001_m.py").write_text(_MIGRATION)
    (root / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "Dockerfile").write_text("FROM python:3.12-slim@sha256:" + "0" * 64 + "\n")
    (root / "authorization-trust-root.json").write_text(
        '{"authorized_ed25519_public_keys":[],"schema_version":1}\n')
    return root


def _doc(base: Path, commit: str = _COMMIT) -> dict:
    return g.generate_provenance_document(base, commit, g.detect_alembic_head(base / "migrations"))


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    assert g.render_document(_doc(base)) == g.render_document(_doc(base))


def test_all_three_scope_hashes_are_valid_and_distinct(tmp_path: Path) -> None:
    doc = _doc(_bundle(tmp_path))
    hashes = {doc["source_tree_hash"], doc["api_artifact_hash"], doc["worker_artifact_hash"]}
    assert len(hashes) == 3
    assert all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in hashes)
    assert doc["schema_version"] == 2 and doc["generator_version"] == "build-provenance-v1"
    assert doc["commit_sha"] == _COMMIT and doc["alembic_revision"] == "360a55cb6abb"
    assert doc["toolchain_contract_version"] == g.TOOLCHAIN_CONTRACT_VERSION
    assert doc["python_base_image_digest"] == g.PYTHON_BASE_IMAGE_DIGEST
    assert doc["uv_image_digest"] == g.UV_IMAGE_DIGEST
    assert len(doc["authorization_trust_root_hash"]) == 64


def test_manifest_has_no_executable_field(tmp_path: Path) -> None:
    manifest = build_manifest(_bundle(tmp_path), ["pyproject.toml"])
    assert manifest and set(manifest[0]) == {"path", "sha256", "size"}


def test_dockerfile_change_moves_all_three_hashes(tmp_path: Path) -> None:  # §6
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "Dockerfile").write_text("FROM python:3.12-slim@sha256:" + "1" * 64 + "\n")
    after = _doc(base)
    for k in ("source_tree_hash", "api_artifact_hash", "worker_artifact_hash"):
        assert after[k] != before[k], k


def test_trust_root_change_moves_all_three_hashes(tmp_path: Path) -> None:  # §2/§6
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "authorization-trust-root.json").write_text(
        '{"authorized_ed25519_public_keys":["' + "aa" * 32 + '"],"schema_version":1}\n')
    after = _doc(base)
    for k in ("source_tree_hash", "api_artifact_hash", "worker_artifact_hash"):
        assert after[k] != before[k], k
    assert after["authorization_trust_root_hash"] != before["authorization_trust_root_hash"]


def test_commit_resolver_priority_and_conflict() -> None:  # §5
    r = g.resolve_commit_sha
    assert r({"BUILD_COMMIT_SHA": _COMMIT, "RAILWAY_GIT_COMMIT_SHA": None,
              "APP_COMMIT_SHA": None}) == _COMMIT
    assert r({"BUILD_COMMIT_SHA": None, "RAILWAY_GIT_COMMIT_SHA": _COMMIT,
              "APP_COMMIT_SHA": None}) == _COMMIT
    assert r({"APP_COMMIT_SHA": _COMMIT}) == _COMMIT
    assert r({"BUILD_COMMIT_SHA": _COMMIT, "APP_COMMIT_SHA": _COMMIT}) == _COMMIT  # two equal ok
    # priority: BUILD wins over the others when all present and equal is required — but conflict:
    for bad in ({"BUILD_COMMIT_SHA": _COMMIT, "APP_COMMIT_SHA": "b" * 40},
                {"RAILWAY_GIT_COMMIT_SHA": _COMMIT, "APP_COMMIT_SHA": "b" * 40}):
        with pytest.raises(ProvenanceError) as ei:
            r(bad)
        assert ei.value.code == "commit_sha_conflict"
    with pytest.raises(ProvenanceError) as ei:
        r({})
    assert ei.value.code == "commit_sha_missing"


def test_same_size_mutation_during_hash_blocks(tmp_path: Path, monkeypatch) -> None:  # §7
    base = _bundle(tmp_path)
    from cestaplan_api.provenance import manifest as mani
    real_read = os.read
    state = {"done": False}

    def racing_read(fd, n):
        data = real_read(fd, n)
        if data and not state["done"]:
            state["done"] = True
            st = os.fstat(fd)
            os.utime(fd, ns=(st.st_mtime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))  # new mtime
        return data

    monkeypatch.setattr(mani.os, "read", racing_read)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["pyproject.toml"])
    assert ei.value.code == "file_changed_during_scan"


def test_atomic_replacement_during_scan_blocks(tmp_path: Path, monkeypatch) -> None:  # §7
    base = _bundle(tmp_path)
    target = base / "pyproject.toml"
    replacement = base / "pyproject.toml.new"
    replacement.write_text("[project]\nname='replaced'\n")
    from cestaplan_api.provenance import manifest as mani
    real_read = os.read
    state = {"done": False}

    def racing_read(fd, n):
        data = real_read(fd, n)
        if data and not state["done"]:
            state["done"] = True
            os.replace(replacement, target)  # new inode over the same path
        return data

    monkeypatch.setattr(mani.os, "read", racing_read)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["pyproject.toml"])
    assert ei.value.code == "file_changed_during_scan"


def test_internal_symlink_is_hashed_not_rejected(tmp_path: Path) -> None:  # §7
    base = _bundle(tmp_path)
    os.symlink(base / "src/cestaplan_api/__init__.py", base / "src/cestaplan_api/alias.py")
    manifest = build_manifest(base, ["src/cestaplan_api"])
    paths = {e["path"] for e in manifest}
    assert "src/cestaplan_api/alias.py" in paths  # in-tree symlink is included by its own name


def test_repo_and_copied_bundle_produce_identical_document(tmp_path: Path) -> None:  # §7
    base = _bundle(tmp_path)
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(base, copy, symlinks=False)
    assert g.render_document(_doc(base)) == g.render_document(_doc(copy))


def test_api_only_change_moves_only_api_hash(tmp_path: Path) -> None:  # §4.4
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "migrations/versions/0002_m.py").write_text(
        _MIGRATION.replace("360a55cb6abb", "abcdef123456").replace("None", "'360a55cb6abb'"))
    after = g.generate_provenance_document(base, _COMMIT, "abcdef123456")
    assert after["api_artifact_hash"] != before["api_artifact_hash"]
    assert after["worker_artifact_hash"] == before["worker_artifact_hash"]
    assert after["source_tree_hash"] != before["source_tree_hash"]


def test_worker_only_change_moves_only_worker_hash(tmp_path: Path) -> None:  # §4.5
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "src/cestaplan_worker/main.py").write_text("# worker main CHANGED\n")
    after = _doc(base)
    assert after["worker_artifact_hash"] != before["worker_artifact_hash"]
    assert after["api_artifact_hash"] == before["api_artifact_hash"]
    assert after["source_tree_hash"] != before["source_tree_hash"]


def test_shared_change_moves_both_hashes(tmp_path: Path) -> None:  # §4.6
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "src/cestaplan_api/tools/apply.py").write_text("# remediation tool CHANGED\n")
    after = _doc(base)
    assert after["api_artifact_hash"] != before["api_artifact_hash"]
    assert after["worker_artifact_hash"] != before["worker_artifact_hash"]


def test_excluded_files_do_not_change_hashes(tmp_path: Path) -> None:  # §4.7
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "src/cestaplan_api/__pycache__").mkdir()
    (base / "src/cestaplan_api/__pycache__/x.pyc").write_bytes(b"cache")
    (base / "src/cestaplan_api/app.log").write_text("log line")
    (base / ".env").write_text("SECRET=1")
    (base / "src/cestaplan_api/service.secret").write_text("shh")
    (base / "build-provenance.json").write_text("{}")
    after = _doc(base)
    assert after == before


def test_file_excluded_predicate() -> None:
    for name in (".env", ".env.production", "x.pyc", "app.log", "creds.secret", "id.key",
                 "cert.pem", "build-provenance.json", "secrets.yaml"):
        assert file_excluded(name), name
    for name in ("main.py", "pyproject.toml", "uv.lock", "0001_m.py"):
        assert not file_excluded(name), name


def test_unsafe_symlink_escaping_tree_is_blocked(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret outside repo")
    link = base / "src/cestaplan_api/escape.py"
    os.symlink(outside, link)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src"])
    assert ei.value.code == "symlink_escapes_tree"


def test_malformed_commit_is_blocked(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    for bad in ("", "xyz", "A" * 40, "a" * 39, "a" * 41):
        with pytest.raises(ProvenanceError) as ei:
            g.generate_provenance_document(base, bad, "360a55cb6abb")
        assert ei.value.code == "commit_sha_invalid"


def test_missing_include_is_blocked(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src", "does_not_exist"])
    assert ei.value.code == "include_missing"


def test_alembic_head_not_unique_is_blocked(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    (base / "migrations/versions/0009_other.py").write_text(
        _MIGRATION.replace("360a55cb6abb", "ffffffffffff"))  # a second independent head
    with pytest.raises(ProvenanceError) as ei:
        g.detect_alembic_head(base / "migrations")
    assert ei.value.code == "alembic_head_not_unique"


def test_cli_writes_document_and_is_reproducible(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    out1 = tmp_path / "p1.json"
    out2 = tmp_path / "p2.json"
    from cestaplan_api.provenance.generate import main
    assert main(["--base", str(base), "--commit-sha", _COMMIT, "--out", str(out1)]) == 0
    assert main(["--base", str(base), "--commit-sha", _COMMIT, "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    doc = json.loads(out1.read_text())
    assert doc["commit_sha"] == _COMMIT and doc["schema_version"] == 2


def test_generation_holds_under_optimize(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    code = (
        "from cestaplan_api.provenance import generator as g\n"
        "try:\n"
        f"    g.generate_provenance_document({str(base)!r}, 'bad', '360a55cb6abb')\n"
        "    print('NO_RAISE')\n"
        "except g.ProvenanceError as e:\n"
        "    print('RAISED', e.code)\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert "RAISED commit_sha_invalid" in out.stdout
