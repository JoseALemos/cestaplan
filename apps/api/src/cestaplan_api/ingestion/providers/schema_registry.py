"""Versioned provider-schema registry on the filesystem (spec §L).

Stores each fetched schema under ``<base>/<provider>/vN/`` with a metadata record, and grades
drift against the previous version. An incompatible change never silently replaces the prior
schema: a new version is always written and marked ``review_required`` / ``breaking`` for a
human to sign off. Default base is ``data/provider-schemas`` (git-tracked, no secrets); tests
pass a temporary base.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cestaplan_api.ingestion.providers.schema_tools import classify_drift, diff_json

DEFAULT_BASE = Path("data/provider-schemas")


@dataclass(slots=True)
class SchemaVersionMeta:
    provider: str
    version: int
    source_url: str
    fetched_at: str
    sha256: str
    openapi_version: str
    provider_schema_version: str
    previous_sha256: str | None
    compatibility_status: str  # unchanged/additive_compatible/review_required/breaking/unavailable
    reviewed_at: str | None = None
    reviewed_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _provider_dir(base: Path, provider: str) -> Path:
    return base / provider


def _sha256(schema: Any) -> str:
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _versions(base: Path, provider: str) -> list[int]:
    pdir = _provider_dir(base, provider)
    if not pdir.exists():
        return []
    out: list[int] = []
    for child in pdir.iterdir():
        if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit():
            out.append(int(child.name[1:]))
    return sorted(out)


def latest_meta(base: Path, provider: str) -> SchemaVersionMeta | None:
    versions = _versions(base, provider)
    if not versions:
        return None
    meta_path = _provider_dir(base, provider) / f"v{versions[-1]}" / "meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    return SchemaVersionMeta(**data)


def _load_schema(base: Path, provider: str, version: int) -> Any:
    path = _provider_dir(base, provider) / f"v{version}" / "schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def compute_drift(old_schema: Any, new_schema: Any) -> dict[str, Any]:
    """Return the change list + compatibility status between two schemas."""
    changes = diff_json(old_schema, new_schema)
    return {"compatibility_status": classify_drift(changes), "changes": changes}


def store_schema(
    schema: Any,
    provider: str,
    source_url: str,
    *,
    base: Path = DEFAULT_BASE,
    now: datetime,
    reviewed_by: str | None = None,
) -> SchemaVersionMeta:
    """Persist a new schema version + metadata. Never overwrites a prior version."""
    sha = _sha256(schema)
    prev = latest_meta(base, provider)
    if prev is not None and prev.sha256 == sha:
        return prev  # identical schema: nothing new to store

    versions = _versions(base, provider)
    version = (versions[-1] + 1) if versions else 1
    if prev is not None:
        prev_schema = _load_schema(base, provider, versions[-1])
        status = compute_drift(prev_schema, schema)["compatibility_status"]
    else:
        status = "unchanged"

    meta = SchemaVersionMeta(
        provider=provider,
        version=version,
        source_url=source_url,
        fetched_at=now.isoformat(),
        sha256=sha,
        openapi_version=str(schema.get("openapi", "unknown"))
        if isinstance(schema, dict)
        else "unknown",
        provider_schema_version=f"v{version}",
        previous_sha256=prev.sha256 if prev is not None else None,
        compatibility_status=status,
        reviewed_at=None,
        reviewed_by=reviewed_by,
    )
    vdir = _provider_dir(base, provider) / f"v{version}"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (vdir / "meta.json").write_text(
        json.dumps(meta.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return meta


def check_drift(new_schema: Any, provider: str, *, base: Path = DEFAULT_BASE) -> dict[str, Any]:
    """Drift of ``new_schema`` vs the stored latest (``unavailable`` if none stored)."""
    versions = _versions(base, provider)
    if not versions:
        return {"compatibility_status": "unavailable", "changes": []}
    return compute_drift(_load_schema(base, provider, versions[-1]), new_schema)


__all__ = [
    "DEFAULT_BASE",
    "SchemaVersionMeta",
    "check_drift",
    "compute_drift",
    "latest_meta",
    "store_schema",
]
