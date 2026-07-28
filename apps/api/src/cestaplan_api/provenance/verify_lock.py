"""Build-time gate: verify the INSTALLED PostgreSQL 18 client closure matches the committed lock
(apps/api/postgresql-client-18-runtime.lock.json). Runs inside the image during ``docker build``
(the base is python:3.12-slim, so ``python3`` + ``dpkg`` are present). Fail-closed: any version,
platform or lock-integrity discrepancy exits non-zero and stops the build. No fallback."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cestaplan_api.provenance.generator import (
    _read_os_release,
    load_pg_runtime_lock,
    verify_pg_runtime_lock,
)
from cestaplan_api.provenance.manifest import ProvenanceError


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m cestaplan_api.provenance.verify_lock <lock.json>", file=sys.stderr)
        return 2
    try:
        lock = load_pg_runtime_lock(Path(argv[1]))
        distribution, codename = _read_os_release()
        arch = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True,
                              check=True, timeout=30).stdout.strip()
        verify_pg_runtime_lock(lock, distribution=distribution, codename=codename,
                               architecture=arch)
    except (ProvenanceError, OSError, subprocess.SubprocessError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"FAIL: pg runtime lock verification failed: {code}", file=sys.stderr)
        return 1
    print("pg runtime lock OK (installed closure matches "
          f"{Path(argv[1]).name}: {', '.join(p['package'] for p in lock['packages'])})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
