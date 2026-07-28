"""CLI that writes the build-provenance document (feat immutable-build-provenance).

Runs at image-build time and in CI over an EXPLICIT ``--base`` (the bundle root that holds ``src``,
``migrations``, ``alembic.ini``, ``pyproject.toml``, ``uv.lock``): ``/app`` in the image,
in the repo. Both layouts are base-relative-identical, so the output is byte-for-byte reproducible.

The build MUST fail if valid evidence cannot be produced (every error is fail-closed and non-zero).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from cestaplan_api.provenance.generator import (
    detect_alembic_head,
    generate_provenance_document,
    measure_pg_client,
    measure_pg_runtime,
    render_document,
    resolve_commit_sha,
)
from cestaplan_api.provenance.manifest import ProvenanceError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=os.environ.get("PROVENANCE_BASE", "."),
                   help="bundle root: src/, migrations/, alembic.ini, pyproject.toml, uv.lock")
    p.add_argument("--commit-sha", default=None,
                   help="explicit override; else resolved from the build env vars")
    p.add_argument("--alembic-revision", default=None,
                   help="override; otherwise resolved from the migration scripts")
    p.add_argument("--out", default=None, help="write the document here (else stdout)")
    p.add_argument("--print-sha256", action="store_true")
    a = p.parse_args(argv)

    base = Path(a.base)
    try:
        # Priority BUILD_COMMIT_SHA > RAILWAY_GIT_COMMIT_SHA > APP_COMMIT_SHA; conflicting values
        # (two present and different) fail the build (§5).
        commit = a.commit_sha.strip() if a.commit_sha else resolve_commit_sha({
            "BUILD_COMMIT_SHA": os.environ.get("BUILD_COMMIT_SHA"),
            "RAILWAY_GIT_COMMIT_SHA": os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
            "APP_COMMIT_SHA": os.environ.get("APP_COMMIT_SHA")})
    except ProvenanceError as exc:
        raise SystemExit(f"FAIL: commit resolution failed: {exc.code}") from exc
    revision = a.alembic_revision or detect_alembic_head(base / "migrations")
    try:
        pg_client = measure_pg_client()   # measured from the installed pg 18 client (fail-closed)
        pg_runtime = measure_pg_runtime()  # full runtime dependency/library closure (fail-closed)
        doc = generate_provenance_document(base, commit, revision, pg_client, pg_runtime)
    except ProvenanceError as exc:
        raise SystemExit(f"FAIL: provenance generation failed: {exc.code}") from exc
    data = render_document(doc)
    if a.out:
        out = Path(a.out)
        out.write_bytes(data)
        out.chmod(0o644)
        if a.print_sha256:
            (out.with_suffix(out.suffix + ".sha256")).write_text(
                hashlib.sha256(data).hexdigest() + "\n")
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
