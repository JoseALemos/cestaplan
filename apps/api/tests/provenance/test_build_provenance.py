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
_PG = {  # synthetic pinned pg 18 client identity (measured for real only inside the image)
    "postgresql_client_package": "postgresql-client-18",
    "postgresql_client_package_version": "18.4-1.pgdg13+1",
    "pg_restore_major": "18",
    "pg_restore_version": "18.4",
    "pg_restore_binary_sha256": "d" * 64,
    "pg_dump_binary_sha256": "e" * 64,
}
# synthetic runtime dependency/library closure (measured for real only inside the image, schema 4)
_PG_RUNTIME_DEPS = [
    {"architecture": "amd64", "package": "libpq5", "version": "18.4-1.pgdg13+1"},
    {"architecture": "amd64", "package": "postgresql-client-18", "version": "18.4-1.pgdg13+1"}]
_PG_RUNTIME_FILES = [
    {"package": "postgresql-client-18", "path": "/usr/lib/postgresql/18/bin/pg_dump",
     "sha256": "a" * 64},
    {"package": "postgresql-client-18", "path": "/usr/lib/postgresql/18/bin/pg_restore",
     "sha256": "b" * 64},
    {"package": "libpq5", "path": "/usr/lib/x86_64-linux-gnu/libpq.so.5.18", "sha256": "c" * 64}]


def _pg_runtime() -> dict:
    core = {"postgresql_runtime_dependencies": [dict(d) for d in _PG_RUNTIME_DEPS],
            "postgresql_runtime_files": [dict(f) for f in _PG_RUNTIME_FILES]}
    return {**core, "postgresql_runtime_manifest_hash": g._pg_runtime_manifest_hash(core)}
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
    (root / "README.md").write_text("# CestaPlan API\n")  # §4v4: a build input in every scope
    (root / "authorization-trust-root.json").write_text(
        '{"authorized_ed25519_public_keys":[],"schema_version":1}\n')
    return root


def _doc(base: Path, commit: str = _COMMIT) -> dict:
    return g.generate_provenance_document(
        base, commit, g.detect_alembic_head(base / "migrations"), _PG, _pg_runtime())


def test_generation_is_byte_deterministic(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    assert g.render_document(_doc(base)) == g.render_document(_doc(base))


def test_all_three_scope_hashes_are_valid_and_distinct(tmp_path: Path) -> None:
    doc = _doc(_bundle(tmp_path))
    hashes = {doc["source_tree_hash"], doc["api_artifact_hash"], doc["worker_artifact_hash"]}
    assert len(hashes) == 3
    assert all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in hashes)
    assert doc["schema_version"] == 4 and doc["generator_version"] == "build-provenance-v1"
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


def test_internal_symlink_is_rejected(tmp_path: Path) -> None:  # §8 — no symlink is followed
    base = _bundle(tmp_path)
    os.symlink(base / "src/cestaplan_api/__init__.py", base / "src/cestaplan_api/alias.py")
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src/cestaplan_api"])
    assert ei.value.code == "symlink_rejected"


def test_symlink_swap_between_stat_and_open_is_rejected(tmp_path: Path, monkeypatch) -> None:  # §8
    base = _bundle(tmp_path)
    target = base / "pyproject.toml"
    outside = tmp_path.parent / "evil.txt"
    outside.write_text("evil")
    from cestaplan_api.provenance import manifest as mani
    real_open = os.open
    state = {"done": False}

    def racing_open(path, flags, *a, **k):
        # simulate a TOCTOU swap: replace the regular file with a symlink just before the real open
        if not state["done"] and str(path).endswith("pyproject.toml"):
            state["done"] = True
            target.unlink()
            os.symlink(outside, target)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(mani.os, "open", racing_open)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["pyproject.toml"])
    assert ei.value.code == "symlink_rejected"  # O_NOFOLLOW refuses the swapped symlink atomically


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
    after = g.generate_provenance_document(base, _COMMIT, "abcdef123456", _PG, _pg_runtime())
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


def test_external_symlink_is_rejected(tmp_path: Path) -> None:  # §8
    base = _bundle(tmp_path)
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret outside repo")
    os.symlink(outside, base / "src/cestaplan_api/escape.py")
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src"])
    assert ei.value.code == "symlink_rejected"


# ---- §3v4: reject ALL symlinks, including directories ----
def test_include_root_symlink_to_directory_is_rejected(tmp_path: Path) -> None:  # §3v4.1
    base = _bundle(tmp_path)
    (base / "realdir").mkdir()
    (base / "realdir/x.py").write_text("# x\n")
    os.symlink(base / "realdir", base / "linkdir", target_is_directory=True)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["linkdir"])
    assert ei.value.code == "symlink_rejected"


def test_include_root_symlink_to_file_is_rejected(tmp_path: Path) -> None:  # §3v4.4
    base = _bundle(tmp_path)
    os.symlink(base / "pyproject.toml", base / "linkfile.toml")
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["linkfile.toml"])
    assert ei.value.code == "symlink_rejected"


def test_nested_directory_symlink_internal_is_rejected(tmp_path: Path) -> None:  # §3v4.2
    base = _bundle(tmp_path)
    os.symlink(base / "src/cestaplan_engine", base / "src/cestaplan_api/enginelink",
               target_is_directory=True)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src"])
    assert ei.value.code == "symlink_rejected"  # a directory symlink is NEVER silently skipped


def test_nested_directory_symlink_external_is_rejected(tmp_path: Path) -> None:  # §3v4.3
    base = _bundle(tmp_path)
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("# secret\n")
    os.symlink(outside, base / "src/cestaplan_api/extlink", target_is_directory=True)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src"])
    assert ei.value.code == "symlink_rejected"


def test_directory_symlink_swap_during_traversal_is_rejected(tmp_path, monkeypatch):  # §3v4.5
    base = _bundle(tmp_path)
    (base / "src/cestaplan_api/sub").mkdir()
    (base / "src/cestaplan_api/sub/y.py").write_text("# y\n")
    outside = tmp_path.parent / "swapdir"
    outside.mkdir(exist_ok=True)
    from cestaplan_api.provenance import manifest as mani
    real_lstat = os.lstat
    state = {"done": False}

    def racing_lstat(path, *a, **k):
        # swap the real nested dir for a symlink just before the manifest's own lstat check
        if not state["done"] and str(path).replace("\\", "/").endswith("cestaplan_api/sub"):
            state["done"] = True
            import shutil
            shutil.rmtree(base / "src/cestaplan_api/sub")
            os.symlink(outside, base / "src/cestaplan_api/sub", target_is_directory=True)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(mani.os, "lstat", racing_lstat)
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["src"])
    assert ei.value.code == "symlink_rejected"


def test_o_nofollow_unavailable_fails_closed(tmp_path: Path, monkeypatch) -> None:  # §3v4.6
    base = _bundle(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # simulate a platform without it
    with pytest.raises(ProvenanceError) as ei:
        build_manifest(base, ["pyproject.toml"])
    assert ei.value.code == "o_nofollow_unavailable"  # never fall back to a symlink-following open


# ---- §4v4: full coverage of build inputs (README + every Dockerfile COPY) ----
def test_readme_change_moves_all_three_hashes(tmp_path: Path) -> None:  # §4v4
    base = _bundle(tmp_path)
    before = _doc(base)
    (base / "README.md").write_text("# CestaPlan API CHANGED\n")
    after = _doc(base)
    for k in ("source_tree_hash", "api_artifact_hash", "worker_artifact_hash"):
        assert after[k] != before[k], k


def test_dockerfile_copies_are_covered() -> None:  # §4v4
    """Every repo file COPYed into /app before the provenance step must be measured by some scope
    (or an explicit, documented allowlist). No uv-affecting input may go unmeasured."""
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    measured = {inc.split("/")[0] for inc in
                (*g.SOURCE_TREE_INCLUDES, *g.API_ARTIFACT_INCLUDES, *g.WORKER_ARTIFACT_INCLUDES)}
    # The APT lock is a build-time verification input (compared to the INSTALLED closure); its
    # identity is bound transitively via the measured postgresql_runtime_* fields, so it need not be
    # in a uv-affecting scope.
    allowlist: set[str] = {"postgresql-client-18-runtime.lock.json"}
    copied: list[str] = []
    for line in dockerfile.splitlines():
        s = line.strip()
        if not s.startswith("COPY ") or "--from=" in s:  # skip stage/base-image copies
            continue
        srcs = s[len("COPY "):].split()[:-1]  # last token is the destination
        copied.extend(src.split("/")[-1] for src in srcs)
    assert copied  # sanity: we actually parsed COPY lines
    for name in copied:
        assert name in measured or name in allowlist, f"{name} copied to /app but not measured"


def test_malformed_commit_is_blocked(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    for bad in ("", "xyz", "A" * 40, "a" * 39, "a" * 41):
        with pytest.raises(ProvenanceError) as ei:
            g.generate_provenance_document(base, bad, "360a55cb6abb", _PG, _pg_runtime())
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


def test_cli_writes_document_and_is_reproducible(tmp_path: Path, monkeypatch) -> None:
    base = _bundle(tmp_path)
    out1 = tmp_path / "p1.json"
    out2 = tmp_path / "p2.json"
    # measure_pg_client reads the real pg 18 client (only present inside the image); inject the
    # synthetic identity so the CLI's determinism is testable without the binaries.
    monkeypatch.setattr("cestaplan_api.provenance.generate.measure_pg_client", lambda: dict(_PG))
    monkeypatch.setattr("cestaplan_api.provenance.generate.measure_pg_runtime", _pg_runtime)
    from cestaplan_api.provenance.generate import main
    assert main(["--base", str(base), "--commit-sha", _COMMIT, "--out", str(out1)]) == 0
    assert main(["--base", str(base), "--commit-sha", _COMMIT, "--out", str(out2)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    doc = json.loads(out1.read_text())
    assert doc["commit_sha"] == _COMMIT and doc["schema_version"] == 4


def test_generation_holds_under_optimize(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    code = (
        "from cestaplan_api.provenance import generator as g\n"
        "try:\n"
        f"    g.generate_provenance_document({str(base)!r}, 'bad', '360a55cb6abb', {{}}, {{}})\n"
        "    print('NO_RAISE')\n"
        "except g.ProvenanceError as e:\n"
        "    print('RAISED', e.code)\n")
    out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, text=True)
    assert "RAISED commit_sha_invalid" in out.stdout


# ---- schema_version 4: pinned PostgreSQL 18 client identity (§5/§8) ----
def test_pg_client_fields_in_document(tmp_path: Path) -> None:
    doc = _doc(_bundle(tmp_path))
    assert doc["postgresql_client_package"] == "postgresql-client-18"
    assert doc["pg_restore_major"] == "18"
    assert doc["pg_restore_version"] == "18.4"
    assert doc["postgresql_client_package_version"] == "18.4-1.pgdg13+1"
    assert doc["pg_restore_binary_sha256"] == "d" * 64
    assert doc["pg_dump_binary_sha256"] == "e" * 64


def test_validate_pg_client_accepts_valid() -> None:
    assert g._validate_pg_client(_PG) == _PG


def test_validate_pg_client_missing_field_blocks() -> None:
    bad = {k: v for k, v in _PG.items() if k != "pg_restore_binary_sha256"}
    with pytest.raises(ProvenanceError):
        g._validate_pg_client(bad)


def test_validate_pg_client_major_17_blocks() -> None:
    with pytest.raises(ProvenanceError) as ei:
        g._validate_pg_client({**_PG, "pg_restore_major": "17", "pg_restore_version": "17.5"})
    assert ei.value.code == "pg_restore_major_invalid"


def test_validate_pg_client_major_19_blocks() -> None:
    with pytest.raises(ProvenanceError):
        g._validate_pg_client({**_PG, "pg_restore_major": "19", "pg_restore_version": "19.1"})


def test_validate_pg_client_wrong_package_blocks() -> None:
    with pytest.raises(ProvenanceError) as ei:
        g._validate_pg_client({**_PG, "postgresql_client_package": "postgresql-client-17"})
    assert ei.value.code == "pg_client_package_invalid"


def test_validate_pg_client_bad_sha_blocks() -> None:
    with pytest.raises(ProvenanceError):
        g._validate_pg_client({**_PG, "pg_restore_binary_sha256": "XY" + "d" * 62})


def test_two_generations_identical_with_pg(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    assert g.render_document(_doc(base)) == g.render_document(_doc(base))


def test_pg_binary_change_moves_document_hash(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    before = g.document_sha256(_doc(base))
    after = g.document_sha256(g.generate_provenance_document(
        base, _COMMIT, g.detect_alembic_head(base / "migrations"),
        {**_PG, "pg_restore_binary_sha256": "f" * 64}, _pg_runtime()))
    assert before != after


# ---- schema_version 4: full runtime dependency/library manifest (§2/§3/§4) ----
def _runtime(deps, files) -> dict:
    core = {"postgresql_runtime_dependencies": deps, "postgresql_runtime_files": files}
    return {**core, "postgresql_runtime_manifest_hash": g._pg_runtime_manifest_hash(core)}


def test_pg_runtime_fields_in_document(tmp_path: Path) -> None:
    doc = _doc(_bundle(tmp_path))
    assert doc["postgresql_runtime_dependencies"] == _PG_RUNTIME_DEPS
    assert doc["postgresql_runtime_files"] == _PG_RUNTIME_FILES
    assert g._SHA256_RE.match(doc["postgresql_runtime_manifest_hash"])
    # the manifest hash is exactly the canonical hash over deps+files
    assert doc["postgresql_runtime_manifest_hash"] == g._pg_runtime_manifest_hash(
        {"postgresql_runtime_dependencies": _PG_RUNTIME_DEPS,
         "postgresql_runtime_files": _PG_RUNTIME_FILES})


def test_pg_runtime_library_change_moves_document_hash(tmp_path: Path) -> None:
    base = _bundle(tmp_path)
    before = g.document_sha256(_doc(base))
    mutated = [dict(f) for f in _PG_RUNTIME_FILES]
    mutated[-1] = {**mutated[-1], "sha256": "f" * 64}  # a single library sha changes
    after = g.document_sha256(g.generate_provenance_document(
        base, _COMMIT, g.detect_alembic_head(base / "migrations"), _PG,
        _runtime([dict(d) for d in _PG_RUNTIME_DEPS], mutated)))
    assert before != after


def test_pg_runtime_two_generations_identical() -> None:
    a = g._pg_runtime_manifest_hash({"postgresql_runtime_dependencies": _PG_RUNTIME_DEPS,
                                     "postgresql_runtime_files": _PG_RUNTIME_FILES})
    b = g._pg_runtime_manifest_hash({"postgresql_runtime_dependencies": _PG_RUNTIME_DEPS,
                                     "postgresql_runtime_files": _PG_RUNTIME_FILES})
    assert a == b


def test_validate_pg_runtime_accepts_valid() -> None:
    rt = _pg_runtime()
    out = g._validate_pg_runtime(rt)
    assert out["postgresql_runtime_manifest_hash"] == rt["postgresql_runtime_manifest_hash"]


def test_validate_pg_runtime_rejects_missing_field() -> None:
    doc = _pg_runtime()
    del doc["postgresql_runtime_files"]
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(doc)
    assert ei.value.code == "pg_runtime_manifest_missing"


def test_validate_pg_runtime_rejects_unordered_deps() -> None:
    deps = list(reversed([dict(d) for d in _PG_RUNTIME_DEPS]))
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime(deps, [dict(f) for f in _PG_RUNTIME_FILES]))
    assert ei.value.code == "pg_runtime_dependencies_unordered"


def test_validate_pg_runtime_rejects_duplicate_dep() -> None:
    deps = [dict(_PG_RUNTIME_DEPS[0]), dict(_PG_RUNTIME_DEPS[0]), dict(_PG_RUNTIME_DEPS[1])]
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime(deps, [dict(f) for f in _PG_RUNTIME_FILES]))
    assert ei.value.code == "pg_runtime_dependencies_unordered"


def test_validate_pg_runtime_rejects_missing_version() -> None:
    deps = [dict(d) for d in _PG_RUNTIME_DEPS]
    deps[0] = {**deps[0], "version": ""}
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime(deps, [dict(f) for f in _PG_RUNTIME_FILES]))
    assert ei.value.code == "pg_runtime_version_invalid"


def test_validate_pg_runtime_rejects_bad_arch() -> None:
    deps = [dict(d) for d in _PG_RUNTIME_DEPS]
    deps[0] = {**deps[0], "architecture": "arm64"}
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime(deps, [dict(f) for f in _PG_RUNTIME_FILES]))
    assert ei.value.code == "pg_runtime_arch_unexpected"


def test_validate_pg_runtime_rejects_bad_sha() -> None:
    files = [dict(f) for f in _PG_RUNTIME_FILES]
    files[0] = {**files[0], "sha256": "nothex"}
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime([dict(d) for d in _PG_RUNTIME_DEPS], files))
    assert ei.value.code == "pg_runtime_sha_invalid"


def test_validate_pg_runtime_rejects_relative_path() -> None:
    files = [dict(f) for f in _PG_RUNTIME_FILES]
    files[0] = {**files[0], "path": "usr/lib/postgresql/18/bin/pg_dump"}  # no leading /
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime([dict(d) for d in _PG_RUNTIME_DEPS], files))
    assert ei.value.code in ("pg_runtime_path_invalid", "pg_runtime_files_unordered")


def test_validate_pg_runtime_rejects_uncovered_binary() -> None:
    files = [dict(f) for f in _PG_RUNTIME_FILES if "pg_restore" not in f["path"]]
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime([dict(d) for d in _PG_RUNTIME_DEPS], files))
    assert ei.value.code == "pg_runtime_binary_uncovered"


def test_validate_pg_runtime_rejects_file_owner_not_a_dependency() -> None:
    files = [dict(f) for f in _PG_RUNTIME_FILES]
    files.append({"package": "libssl3t64", "path": "/usr/lib/x86_64-linux-gnu/libssl.so.3",
                  "sha256": "d" * 64})
    files.sort(key=lambda e: (e["path"], e["package"]))
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(_runtime([dict(d) for d in _PG_RUNTIME_DEPS], files))
    assert ei.value.code == "pg_runtime_file_uncovered"


def test_validate_pg_runtime_rejects_wrong_manifest_hash() -> None:
    doc = _pg_runtime()
    doc["postgresql_runtime_manifest_hash"] = "0" * 64
    with pytest.raises(g.ProvenanceError) as ei:
        g._validate_pg_runtime(doc)
    assert ei.value.code == "pg_runtime_manifest_hash_mismatch"


# ---- schema_version 4: APT runtime lock (§5) ----
def test_pg_runtime_lock_self_consistent_and_matches_dockerfile() -> None:
    lock = g.load_pg_runtime_lock(Path("postgresql-client-18-runtime.lock.json"))
    assert lock["distribution"] == "debian" and lock["codename"] == "trixie"
    assert lock["architecture"] == "amd64"
    names = [p["package"] for p in lock["packages"]]
    assert names == sorted(names)
    assert {"libpq5", "postgresql-client-18", "postgresql-client-common"} == set(names)
    # the pinned client version matches the Dockerfile ARG default
    df = Path("Dockerfile").read_text()
    for pkg in lock["packages"]:
        assert pkg["version"] in df


def test_pg_runtime_lock_rejects_tampered_hash(tmp_path: Path) -> None:
    import json as _json
    lock = _json.loads(Path("postgresql-client-18-runtime.lock.json").read_bytes())
    lock["dependency_closure_hash"] = "0" * 64
    p = tmp_path / "lock.json"
    p.write_text(_json.dumps(lock))
    with pytest.raises(g.ProvenanceError) as ei:
        g.load_pg_runtime_lock(p)
    assert ei.value.code == "pg_lock_hash_mismatch"
