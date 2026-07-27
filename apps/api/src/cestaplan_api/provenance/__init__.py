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
    load_authorization_package_from_files,
)
from cestaplan_api.provenance.generator import (
    BUILD_AUTHORIZATION_TRUST_ROOT_PATH,
    EVIDENCE_DOCUMENT_SCHEMA_VERSION,
    GENERATOR_VERSION,
    TOOLCHAIN_CONTRACT_VERSION,
    canonical_json,
    generate_provenance_document,
    render_document,
    resolve_commit_sha,
)
from cestaplan_api.provenance.manifest import ProvenanceError, build_manifest, manifest_hash
from cestaplan_api.provenance.trust_root import (
    TrustRootError,
    load_trust_root,
    parse_trust_root,
    trust_root_hash,
)

__all__ = [
    "BUILD_AUTHORIZATION_TRUST_ROOT_PATH",
    "EVIDENCE_DOCUMENT_SCHEMA_VERSION",
    "GENERATOR_VERSION",
    "TOOLCHAIN_CONTRACT_VERSION",
    "AuthorizationError",
    "AuthorizationPackage",
    "ProvenanceError",
    "TrustRootError",
    "build_manifest",
    "canonical_json",
    "generate_provenance_document",
    "load_authorization_package",
    "load_authorization_package_from_files",
    "load_trust_root",
    "manifest_hash",
    "parse_trust_root",
    "render_document",
    "resolve_commit_sha",
    "trust_root_hash",
]
