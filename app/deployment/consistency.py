"""Fail-closed deployment identity and artifact compatibility checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Mapping, Sequence

from app.data.v2_schema import CANONICAL_V2_SCHEMA_VERSION
from app.data.v2_version import CANONICAL_V2_TRANSFORMER_VERSION
from app.deployment.artifacts import (
    GIT_COMMIT_PATTERN,
    load_and_verify_production_manifest,
)
from app.graph.v2 import V2_GRAPH_PROJECTION_VERSION
from app.search.store import SemanticIndexStore
from app.search.v2 import V2_SEMANTIC_PROJECTION_VERSION


REQUIRED_READY_HEALTH = {
    "status": "ok",
    "readiness_status": "READY",
    "semantic_index_readiness": "READY",
    "compatibility_status": "READY",
    "production_artifacts_readiness": "READY",
}


class DeploymentConsistencyError(RuntimeError):
    """The selected code, configuration, and artifacts are not one release."""


def validate_deployment_preflight(
    *,
    code_sha: str,
    image_ref: str,
    app_git_commit: str,
    agent_image_tag: str,
    artifact_release_id: str,
    host_artifact_root: str,
    release_base: str,
    mounted_artifact_root: Path,
    environment: Mapping[str, str],
) -> None:
    """Validate effective deployment identity before Compose starts services."""

    try:
        _validate_code_identity(
            code_sha=code_sha,
            image_ref=image_ref,
            app_git_commit=app_git_commit,
            agent_image_tag=agent_image_tag,
        )
        _validate_artifact_identity(
            artifact_release_id=artifact_release_id,
            host_artifact_root=host_artifact_root,
            release_base=release_base,
        )

        generation = environment.get("CANONICAL_V2_GENERATION", "260824")
        snapshot = environment.get("DATA_SNAPSHOT_DATE", "2026-08-24")
        ontology_version = environment.get(
            "CANONICAL_V2_ONTOLOGY_VERSION", "merged-optical-1.4"
        )
        transformer_version = environment.get(
            "CANONICAL_V2_TRANSFORMER_VERSION",
            CANONICAL_V2_TRANSFORMER_VERSION,
        )
        graph_version = environment.get(
            "CANONICAL_V2_GRAPH_PROJECTION_VERSION",
            V2_GRAPH_PROJECTION_VERSION,
        )
        semantic_version = environment.get(
            "CANONICAL_V2_SEMANTIC_INDEX_VERSION",
            V2_SEMANTIC_PROJECTION_VERSION,
        )

        release = load_and_verify_production_manifest(
            mounted_artifact_root / "release.json",
            mounted_artifact_root,
            canonical_dataset_version=generation,
            ontology_version=ontology_version,
            graph_version=graph_version,
            semantic_artifact_version=semantic_version,
            expected_release_id=artifact_release_id,
        )
        if release.artifact_manifest.cutoff != snapshot:
            raise RuntimeError(
                "artifact snapshot does not match runtime configuration: "
                f"manifest={release.artifact_manifest.cutoff!r} "
                f"runtime={snapshot!r}"
            )

        semantic_artifact = next(
            item
            for item in release.artifact_manifest.artifacts
            if item.role == "semantic_index"
        )
        if semantic_artifact.relative_path != "data/semantic_search.json":
            raise RuntimeError(
                "semantic index must be data/semantic_search.json in the release"
            )
        semantic_path = mounted_artifact_root / semantic_artifact.relative_path
        derived = SemanticIndexStore(semantic_path).validate_derived_manifest(
            generation=generation,
            snapshot=snapshot,
            ontology_version=ontology_version,
            canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
            transformer_version=transformer_version,
            projection_version=semantic_version,
        )
        if derived.store_kind != "semantic_index":
            raise RuntimeError(
                "derived manifest store_kind must be 'semantic_index'"
            )
    except (OSError, StopIteration, ValueError, RuntimeError) as exc:
        if isinstance(exc, DeploymentConsistencyError):
            raise
        raise DeploymentConsistencyError(str(exc)) from exc


def validate_health_payload(payload: Mapping[str, Any]) -> None:
    """Require all production readiness gates, not merely an HTTP 200."""

    mismatches = [
        f"{field}={payload.get(field)!r} expected {expected!r}"
        for field, expected in REQUIRED_READY_HEALTH.items()
        if payload.get(field) != expected
    ]
    if mismatches:
        raise DeploymentConsistencyError(
            "deployment health is not READY: " + "; ".join(mismatches)
        )


def _validate_code_identity(
    *,
    code_sha: str,
    image_ref: str,
    app_git_commit: str,
    agent_image_tag: str,
) -> None:
    if not GIT_COMMIT_PATTERN.fullmatch(code_sha):
        raise DeploymentConsistencyError(
            "deployment code SHA must be a lowercase 40-character Git SHA"
        )
    image_name, separator, image_tag = image_ref.rpartition(":")
    if not separator or not image_name or not image_tag or "@" in image_ref:
        raise DeploymentConsistencyError(
            "image reference must use an immutable SHA tag"
        )
    identities = {
        "deployment code SHA": code_sha,
        "image tag": image_tag,
        "AGENT_IMAGE_TAG": agent_image_tag,
        "APP_GIT_COMMIT": app_git_commit,
    }
    if any(value != code_sha for value in identities.values()):
        rendered = ", ".join(f"{name}={value!r}" for name, value in identities.items())
        raise DeploymentConsistencyError(
            "code image identity mismatch: " + rendered
        )


def _validate_artifact_identity(
    *, artifact_release_id: str, host_artifact_root: str, release_base: str
) -> None:
    if not artifact_release_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in artifact_release_id
    ):
        raise DeploymentConsistencyError("artifact release id is invalid")
    expected = PurePosixPath(release_base) / artifact_release_id
    actual = PurePosixPath(host_artifact_root)
    if actual != expected or actual.name != artifact_release_id:
        raise DeploymentConsistencyError(
            "artifact release identity mismatch: "
            f"release_id={artifact_release_id!r} root={host_artifact_root!r} "
            f"expected={str(expected)!r}"
        )


def _preflight_command(args: argparse.Namespace) -> None:
    validate_deployment_preflight(
        code_sha=args.code_sha,
        image_ref=args.image_ref,
        app_git_commit=os.environ.get("APP_GIT_COMMIT", ""),
        agent_image_tag=os.environ.get("AGENT_IMAGE_TAG", ""),
        artifact_release_id=os.environ.get("ARTIFACT_RELEASE_ID", ""),
        host_artifact_root=args.host_artifact_root,
        release_base=args.release_base,
        mounted_artifact_root=args.artifact_root,
        environment=os.environ,
    )
    print(
        "deployment preflight READY: "
        f"code_sha={args.code_sha} "
        f"artifact_release_id={os.environ['ARTIFACT_RELEASE_ID']}"
    )


def _health_command() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeploymentConsistencyError("health response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise DeploymentConsistencyError("health response must be a JSON object")
    validate_health_payload(payload)
    print("deployment health READY")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--code-sha", required=True)
    preflight.add_argument("--image-ref", required=True)
    preflight.add_argument("--host-artifact-root", required=True)
    preflight.add_argument("--release-base", required=True)
    preflight.add_argument(
        "--artifact-root", type=Path, default=Path("/var/lib/financial-semantic-agent")
    )
    subparsers.add_parser("health")
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            _preflight_command(args)
        else:
            _health_command()
    except DeploymentConsistencyError as exc:
        print(f"deployment consistency check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
