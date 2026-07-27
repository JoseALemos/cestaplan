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
    render_document,
)
from cestaplan_api.provenance.manifest import ProvenanceError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=os.environ.get("PROVENANCE_BASE", "."),
                   help="bundle root: src/, migrations/, alembic.ini, pyproject.toml, uv.lock")
    p.add_argument("--commit-sha", default=os.environ.get("APP_COMMIT_SHA")
                   or os.environ.get("RAILWAY_GIT_COMMIT_SHA"))
    p.add_argument("--alembic-revision", default=None,
                   help="override; otherwise resolved from the migration scripts")
    p.add_argument("--out", default=None, help="write the document here (else stdout)")
    p.add_argument("--print-sha256", action="store_true")
    a = p.parse_args(argv)

    base = Path(a.base)
    commit = a.commit_sha
    if not commit:
        raise SystemExit("FAIL: commit sha missing (set APP_COMMIT_SHA or --commit-sha)")
    revision = a.alembic_revision or detect_alembic_head(base / "migrations")
    try:
        doc = generate_provenance_document(base, commit, revision)
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
