"""Fail-closed production artifact bundle manifest validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


REQUIRED_ARTIFACT_ROLES = frozenset({
    "canonical_source",
    "ontology",
    "kodex_holdings",
    "tiger_holdings",
    "krx_issuers",
    "ishares_holdings",
    "ishares_returns",
    "semantic_index",
})


class DeploymentArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    version: str
    relative_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["file", "directory"] = "file"
    effective_date: str
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility_version: str
    required: Literal[True] = True

    @model_validator(mode="after")
    def safe_relative_path(self) -> "DeploymentArtifact":
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("artifact path must be a safe relative path")
        return self


class ProductionArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["m10.9-c3.2-production-artifacts-v1"]
    release_status: Literal["READY"]
    deployment_version: str
    release_id: str
    git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    cutoff: Literal["2026-08-24"]
    canonical_dataset_version: str
    ontology_version: str
    graph_version: str
    semantic_artifact_version: str
    artifacts: list[DeploymentArtifact]

    @model_validator(mode="after")
    def complete_roles(self) -> "ProductionArtifactManifest":
        roles = [item.role for item in self.artifacts]
        if len(roles) != len(set(roles)):
            raise ValueError("deployment artifact roles must be unique")
        missing = REQUIRED_ARTIFACT_ROLES - set(roles)
        if missing:
            raise ValueError("deployment manifest missing roles: " + ",".join(sorted(missing)))
        return self


def load_and_verify_production_manifest(
    manifest_path: Path,
    artifact_root: Path,
    *,
    canonical_dataset_version: str,
    ontology_version: str,
    graph_version: str,
    semantic_artifact_version: str,
) -> ProductionArtifactManifest:
    """Validate release versions and every immutable artifact checksum."""

    manifest = ProductionArtifactManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    expected = {
        "canonical_dataset_version": canonical_dataset_version,
        "ontology_version": ontology_version,
        "graph_version": graph_version,
        "semantic_artifact_version": semantic_artifact_version,
    }
    mismatches = [
        f"{field}={getattr(manifest, field)!r} expected {value!r}"
        for field, value in expected.items()
        if getattr(manifest, field) != value
    ]
    if mismatches:
        raise RuntimeError("production artifact versions are incompatible: " + "; ".join(mismatches))

    root = artifact_root.resolve(strict=True)
    for artifact in manifest.artifacts:
        candidate = (root / artifact.relative_path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise RuntimeError(f"artifact escapes configured root: {artifact.role}")
        if artifact.kind == "file" and not candidate.is_file():
            raise RuntimeError(f"artifact is not a file: {artifact.role}")
        if artifact.kind == "directory" and not candidate.is_dir():
            raise RuntimeError(f"artifact is not a directory: {artifact.role}")
        actual = artifact_checksum(candidate)
        if actual != artifact.sha256:
            raise RuntimeError(f"artifact checksum mismatch: {artifact.role}")
    return manifest


def artifact_checksum(path: Path) -> str:
    """Hash a file or a directory tree without timestamps or platform metadata."""

    digest = hashlib.sha256()
    if path.is_file():
        _update_file(digest, path)
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"artifact does not exist: {path}")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    for item in files:
        relative = item.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        _update_file(digest, item)
    return digest.hexdigest()


def write_manifest(path: Path, manifest: ProductionArtifactManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _update_file(digest: "hashlib._Hash", path: Path) -> None:
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
