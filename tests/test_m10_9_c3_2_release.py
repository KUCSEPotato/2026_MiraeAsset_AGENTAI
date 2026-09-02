from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from app.deployment.artifacts import (
    REQUIRED_ARTIFACT_ROLES,
    artifact_checksum,
    load_and_verify_production_manifest,
)


VERSIONS = {
    "canonical_dataset_version": "260824",
    "ontology_version": "merged-optical-1.4",
    "graph_version": "m10.9-c2.8-canonical-v2-graph-5",
    "semantic_artifact_version": "m10.9-c2-canonical-v2-semantic-1",
}


FINAL_SHA = "1" * 40


def _artifact_manifest(root: Path) -> dict:
    artifacts = []
    for role in sorted(REQUIRED_ARTIFACT_ROLES):
        target = root / role
        target.write_text(role, encoding="utf-8")
        artifacts.append({
            "role": role,
            "version": f"{role}-v1",
            "relative_path": role,
            "sha256": artifact_checksum(target),
            "kind": "file",
            "effective_date": "2026-08-24",
            "source_manifest_sha256": artifact_checksum(target),
            "compatibility_version": "test-v1",
            "required": True,
        })
    return {
        "schema_version": "m10.9-c3.3-production-artifacts-v2",
        "release_status": "READY",
        "deployment_version": "c3.2-test",
        "cutoff": "2026-08-24",
        **VERSIONS,
        "artifacts": artifacts,
    }


def _release(root: Path) -> dict:
    return {
        "schema_version": "m10.9-c3.3-release-v1",
        "release_id": "c3.3-test",
        "git_commit": FINAL_SHA,
        "artifact_manifest": _artifact_manifest(root),
    }


def test_production_manifest_requires_all_versions_and_checksums(tmp_path: Path) -> None:
    payload = _release(tmp_path)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_and_verify_production_manifest(
        path, tmp_path, expected_git_commit=FINAL_SHA, **VERSIONS
    )

    assert loaded.git_commit == FINAL_SHA
    assert loaded.artifact_manifest.release_status == "READY"
    assert {
        item.role for item in loaded.artifact_manifest.artifacts
    } == REQUIRED_ARTIFACT_ROLES


def test_production_manifest_rejects_checksum_drift(tmp_path: Path) -> None:
    payload = _release(tmp_path)
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "kodex_holdings").write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch: kodex_holdings"):
        load_and_verify_production_manifest(
            path, tmp_path, expected_git_commit=FINAL_SHA, **VERSIONS
        )


def test_production_manifest_rejects_version_drift(tmp_path: Path) -> None:
    payload = _release(tmp_path)
    payload["artifact_manifest"]["ontology_version"] = "stale"
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="versions are incompatible"):
        load_and_verify_production_manifest(
            path, tmp_path, expected_git_commit=FINAL_SHA, **VERSIONS
        )


def test_release_git_sha_is_bundle_only_and_must_match_image(tmp_path: Path) -> None:
    artifact_manifest = _artifact_manifest(tmp_path)
    assert "git_commit" not in artifact_manifest
    assert "release_id" not in artifact_manifest
    payload = {
        "schema_version": "m10.9-c3.3-release-v1",
        "release_id": "post-commit-release",
        "git_commit": FINAL_SHA,
        "artifact_manifest": artifact_manifest,
    }
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="does not match the running image"):
        load_and_verify_production_manifest(
            path,
            tmp_path,
            expected_git_commit="2" * 40,
            **VERSIONS,
        )


def test_tracked_manifest_never_embeds_a_code_commit() -> None:
    tracked = json.loads(
        Path("deployment/production-artifacts.json").read_text(encoding="utf-8")
    )
    assert "git_commit" not in tracked
    assert "release_id" not in tracked
    assert tracked["schema_version"] == "m10.9-c3.3-production-artifacts-v2"


def test_packager_generates_bundle_only_release_for_final_commit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    artifact_manifest = _artifact_manifest(root)
    manifest_path = tmp_path / "production-artifacts.json"
    manifest_path.write_text(json.dumps(artifact_manifest), encoding="utf-8")
    bundle_path = tmp_path / "release.tar"

    subprocess.run(
        [
            sys.executable,
            "scripts/package_production_bundle.py",
            "--bundle-root",
            str(root),
            "--manifest",
            str(manifest_path),
            "--output",
            str(bundle_path),
            "--release-id",
            "post-commit-release",
            "--git-commit",
            FINAL_SHA,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with tarfile.open(bundle_path) as archive:
        release = json.load(archive.extractfile("release.json"))
        tracked = json.load(
            archive.extractfile("manifests/production-artifacts.json")
        )
    assert release["git_commit"] == FINAL_SHA
    assert release["artifact_manifest"] == tracked
    assert "git_commit" not in tracked


def test_deployment_workflow_is_test_gated_immutable_and_kill_switched() -> None:
    workflow = Path(".github/workflows/deploy-production.yml").read_text()
    assert "needs: test" in workflow
    assert "needs: image" in workflow
    assert "vars.DEPLOY_ENABLED == 'true'" in workflow
    assert "financial-semantic-agent:${GITHUB_SHA}" in workflow
    assert "secrets.DEPLOY_SSH_KEY" in workflow
    assert "secrets.DEPLOY_HOST_KEY" in workflow
    assert ":latest" not in workflow


def test_naver_deploy_requires_bundle_checksum_and_health_before_promotion() -> None:
    script = Path("scripts/deploy_naver.sh").read_text()
    checksum = script.index("sha256sum -c")
    live = script.index("/live")
    ready = script.index("/health")
    smoke = script.index("/answer")
    promote = script.index('ln -sfn "$release_dir" "$base/current"')
    assert checksum < live < ready < smoke < promote
    assert "trap rollback ERR" in script
