"""Build a checksum-pinned C3.2 production artifact manifest."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.deployment.artifacts import (  # noqa: E402
    DeploymentArtifact,
    ProductionArtifactManifest,
    artifact_checksum,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment-version", required=True)
    parser.add_argument("--canonical-version", default="260824")
    parser.add_argument("--ontology-version", default="merged-optical-1.4")
    parser.add_argument(
        "--graph-version", default="m10.9-c2.8-canonical-v2-graph-5"
    )
    parser.add_argument(
        "--semantic-version", default="m10.9-c2-canonical-v2-semantic-1"
    )
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar=(
            "ROLE=VERSION=KIND=RELATIVE_PATH=EFFECTIVE_DATE="
            "COMPATIBILITY_VERSION=SOURCE_MANIFEST_PATH"
        ),
    )
    args = parser.parse_args()

    root = args.artifact_root.resolve(strict=True)
    artifacts = []
    for raw in args.artifact:
        try:
            (
                role,
                version,
                kind,
                relative_path,
                effective_date,
                compatibility_version,
                source_manifest_path,
            ) = raw.split("=", 6)
        except ValueError as exc:
            raise SystemExit(f"invalid --artifact value: {raw}") from exc
        target = (root / relative_path).resolve(strict=True)
        if not target.is_relative_to(root):
            raise SystemExit(f"artifact escapes root: {role}")
        source_manifest = (root / source_manifest_path).resolve(strict=True)
        if not source_manifest.is_relative_to(root) or not source_manifest.is_file():
            raise SystemExit(f"invalid source manifest: {role}")
        artifacts.append(DeploymentArtifact(
            role=role,
            version=version,
            relative_path=relative_path,
            kind=kind,
            sha256=artifact_checksum(target),
            effective_date=effective_date,
            compatibility_version=compatibility_version,
            source_manifest_sha256=hashlib.sha256(
                source_manifest.read_bytes()
            ).hexdigest(),
            required=True,
        ))

    manifest = ProductionArtifactManifest(
        schema_version="m10.9-c3.3-production-artifacts-v2",
        release_status="READY",
        deployment_version=args.deployment_version,
        cutoff="2026-08-24",
        canonical_dataset_version=args.canonical_version,
        ontology_version=args.ontology_version,
        graph_version=args.graph_version,
        semantic_artifact_version=args.semantic_version,
        artifacts=artifacts,
    )
    write_manifest(args.output, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
