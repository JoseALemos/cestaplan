"""Immutable build provenance: deterministic file manifests, the build-provenance document
generator, and the sealed authorization-package loader (feat immutable-build-provenance).

This package produces reproducible, fail-closed evidence about the runtime bundle that built an
image, and validates a future signed authorization package. It never records timestamps, absolute
paths, owners, secrets or any non-reproducible metadata.
"""

from __future__ import annotations

from cestaplan_api.provenance.authorization import (
    AuthorizationError,
    AuthorizationPackage,
    load_authorization_package,
)
from cestaplan_api.provenance.generator import (
    EVIDENCE_DOCUMENT_SCHEMA_VERSION,
    GENERATOR_VERSION,
    canonical_json,
    generate_provenance_document,
)
from cestaplan_api.provenance.manifest import ProvenanceError, build_manifest, manifest_hash

__all__ = [
    "EVIDENCE_DOCUMENT_SCHEMA_VERSION",
    "GENERATOR_VERSION",
    "AuthorizationError",
    "AuthorizationPackage",
    "ProvenanceError",
    "build_manifest",
    "canonical_json",
    "generate_provenance_document",
    "load_authorization_package",
    "manifest_hash",
]
