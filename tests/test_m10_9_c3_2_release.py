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
    validate_code_commit,
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
        path, tmp_path, expected_release_id="c3.3-test", **VERSIONS
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
            path, tmp_path, expected_release_id="c3.3-test", **VERSIONS
        )


def test_production_manifest_rejects_version_drift(tmp_path: Path) -> None:
    payload = _release(tmp_path)
    payload["artifact_manifest"]["ontology_version"] = "stale"
    path = tmp_path / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="versions are incompatible"):
        load_and_verify_production_manifest(
            path, tmp_path, expected_release_id="c3.3-test", **VERSIONS
        )


def test_artifact_release_is_independent_from_the_running_code_sha(tmp_path: Path) -> None:
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

    loaded = load_and_verify_production_manifest(
        path,
        tmp_path,
        expected_release_id="post-commit-release",
        **VERSIONS,
    )
    assert loaded.git_commit == FINAL_SHA
    assert validate_code_commit("2" * 40) == "2" * 40


def test_artifact_release_rejects_a_different_configured_identity(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps(_release(tmp_path)), encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact release id does not match"):
        load_and_verify_production_manifest(
            path,
            tmp_path,
            expected_release_id="another-release",
            **VERSIONS,
        )


def test_code_commit_identity_requires_an_exact_lowercase_sha() -> None:
    assert validate_code_commit(FINAL_SHA) == FINAL_SHA
    for invalid in ("1" * 39, "1" * 41, "G" * 40, "ABCDEF" * 6 + "ABCD"):
        with pytest.raises(RuntimeError, match="APP_GIT_COMMIT"):
            validate_code_commit(invalid)


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
    assert "uv run python -m pytest -p no:capture -p no:debugging" in workflow
    assert "uv run pytest -p no:capture -p no:debugging" not in workflow
    assert "needs: test" in workflow
    assert "needs: [image, frontend-image]" in workflow
    assert "financial-semantic-frontend:${GITHUB_SHA}" in workflow
    assert "bash scripts/test_frontend_container.sh" in workflow
    assert "vars.DEPLOY_ENABLED == 'true'" in workflow
    assert "vars.ARTIFACT_RELEASE_ID" in workflow
    assert "financial-semantic-agent:${GITHUB_SHA}" in workflow
    assert "secrets.NAVER_DEPLOY_HOST" in workflow
    assert "secrets.NAVER_DEPLOY_USER" in workflow
    assert "secrets.NAVER_DEPLOY_SSH_KEY" in workflow
    assert "secrets.NAVER_DEPLOY_HOST_KEY" in workflow
    assert "'$CODE_SHA' '$IMAGE' '$ARTIFACT_RELEASE_ID'" in workflow
    assert ":latest" not in workflow


def test_naver_deploy_requires_bundle_checksum_and_health_before_promotion() -> None:
    script = Path("scripts/deploy_naver.sh").read_text()
    checksum = script.index("sha256sum -c")
    live = script.index("/live")
    ready = script.index("/health")
    smoke = script.index("/answer")
    promote = script.index('ln -sfn "$code_release_dir" "$base/current"')
    assert checksum < live < ready < smoke < promote
    assert "trap rollback ERR" in script
    assert 'environment_file="$base/.env"' in script
    assert 'incoming="$base/incoming/$artifact_release_id.tar"' in script
    assert 'artifact_dir="$artifact_release_dir"' in script
    assert 'test -f "$artifact_dir/release.json"' in script
    assert 'export SEMANTIC_ARTIFACT_ROOT="$artifact_dir"' in script
    assert (
        'export SEMANTIC_ARTIFACT_ROOT="$base/releases/'
        '$previous_artifact_release_id"'
    ) in script
    assert '$artifact_release_id/artifacts' not in script
    assert '$previous_artifact_release_id/artifacts' not in script
    assert "tar -xf" not in script
    assert "up -d --no-deps agent-api" in script
    assert "question=" in script
    assert "expected=" in script
    assert 'echo "rollback restored $previous_code_sha"' in script
    assert 'echo "rollback health verification failed"' in script


def test_naver_deploy_supports_bundle_extracted_directly_at_release_root(
    tmp_path: Path,
) -> None:
    release_root = tmp_path / "submission-candidate-20260902-v4"
    release_root.mkdir()
    (release_root / "release.json").write_text("{}\n", encoding="utf-8")
    for directory in ("data", "external_data", "manifests", "material", "ontology"):
        (release_root / directory).mkdir()

    assert {item.name for item in release_root.iterdir()} == {
        "release.json",
        "data",
        "external_data",
        "manifests",
        "material",
        "ontology",
    }
    assert (release_root / "release.json").is_file()
    assert not (release_root / "artifacts").exists()

    script = Path("scripts/deploy_naver.sh").read_text()
    assert 'artifact_release_dir="$base/releases/$artifact_release_id"' in script
    assert 'artifact_dir="$artifact_release_dir"' in script
    assert 'test -f "$artifact_dir/release.json"' in script


def test_release_binaries_are_excluded_from_git_and_docker_context() -> None:
    gitignore = Path(".gitignore").read_text()
    dockerignore = Path(".dockerignore").read_text()
    for pattern in (
        "mirae-production-artifacts-*.zip",
        "submission-candidate-*.tar",
        "submission-candidate-*.tar.sha256",
    ):
        assert pattern in gitignore
        assert pattern in dockerignore
