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
from app.deployment.consistency import (
    DeploymentConsistencyError,
    validate_deployment_preflight,
    validate_health_payload,
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


def _deployment_release(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    release_id = "deployment-test"
    root = tmp_path / release_id
    root.mkdir()
    semantic_path = root / "data" / "semantic_search.json"
    semantic_path.parent.mkdir()
    semantic_path.write_text(
        json.dumps({
            "format": "semantic-search-json-v1",
            "metadata": {},
            "documents": [],
            "derived_manifest": {
                "store_kind": "semantic_index",
                "status": "READY",
                "generation": "260824",
                "snapshot": "2026-08-24",
                "ontology_version": "merged-optical-1.4",
                "canonical_schema_version": "m10.9-c2.6-canonical-v2",
                "transformer_version": "return-period-metrics-1",
                "projection_version": "m10.9-c2-canonical-v2-semantic-1",
                "document_count": 0,
            },
        }),
        encoding="utf-8",
    )
    artifacts = []
    for role in sorted(REQUIRED_ARTIFACT_ROLES):
        target = semantic_path if role == "semantic_index" else root / role
        if role != "semantic_index":
            target.write_text(role, encoding="utf-8")
        artifacts.append({
            "role": role,
            "version": f"{role}-v1",
            "relative_path": target.relative_to(root).as_posix(),
            "sha256": artifact_checksum(target),
            "kind": "file",
            "effective_date": "2026-08-24",
            "source_manifest_sha256": artifact_checksum(target),
            "compatibility_version": "test-v1",
            "required": True,
        })
    release = {
        "schema_version": "m10.9-c3.3-release-v1",
        "release_id": release_id,
        "git_commit": FINAL_SHA,
        "artifact_manifest": {
            "schema_version": "m10.9-c3.3-production-artifacts-v2",
            "release_status": "READY",
            "deployment_version": "deployment-test",
            "cutoff": "2026-08-24",
            **VERSIONS,
            "artifacts": artifacts,
        },
    }
    (root / "release.json").write_text(json.dumps(release), encoding="utf-8")
    environment = {
        "CANONICAL_V2_GENERATION": "260824",
        "DATA_SNAPSHOT_DATE": "2026-08-24",
        "CANONICAL_V2_ONTOLOGY_VERSION": "merged-optical-1.4",
        "CANONICAL_V2_TRANSFORMER_VERSION": "return-period-metrics-1",
        "CANONICAL_V2_GRAPH_PROJECTION_VERSION": VERSIONS["graph_version"],
        "CANONICAL_V2_SEMANTIC_INDEX_VERSION": VERSIONS[
            "semantic_artifact_version"
        ],
    }
    return root, environment


def _run_deployment_preflight(
    root: Path,
    environment: dict[str, str],
    *,
    app_git_commit: str = FINAL_SHA,
    agent_image_tag: str = FINAL_SHA,
    host_artifact_root: str = "/opt/mirae-agent/releases/deployment-test",
) -> None:
    validate_deployment_preflight(
        code_sha=FINAL_SHA,
        image_ref=f"ghcr.io/example/agent:{FINAL_SHA}",
        app_git_commit=app_git_commit,
        agent_image_tag=agent_image_tag,
        artifact_release_id="deployment-test",
        host_artifact_root=host_artifact_root,
        release_base="/opt/mirae-agent/releases",
        mounted_artifact_root=root,
        environment=environment,
    )


def _rewrite_semantic_manifest(root: Path, **updates: object) -> None:
    semantic_path = root / "data" / "semantic_search.json"
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    if updates.pop("remove", False):
        semantic.pop("derived_manifest", None)
    else:
        semantic["derived_manifest"].update(updates)
    semantic_path.write_text(json.dumps(semantic), encoding="utf-8")
    release_path = root / "release.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    artifact = next(
        item
        for item in release["artifact_manifest"]["artifacts"]
        if item["role"] == "semantic_index"
    )
    artifact["sha256"] = artifact_checksum(semantic_path)
    artifact["source_manifest_sha256"] = artifact["sha256"]
    release_path.write_text(json.dumps(release), encoding="utf-8")


def test_deployment_preflight_accepts_aligned_code_and_artifact(tmp_path: Path) -> None:
    root, environment = _deployment_release(tmp_path)

    _run_deployment_preflight(root, environment)


def test_deployment_preflight_rejects_old_semantic_artifact(
    tmp_path: Path,
) -> None:
    root, environment = _deployment_release(tmp_path)
    _rewrite_semantic_manifest(root, remove=True)

    with pytest.raises(DeploymentConsistencyError, match="canonical_v2 derived"):
        _run_deployment_preflight(root, environment)


def test_deployment_preflight_rejects_code_identity_mismatch(
    tmp_path: Path,
) -> None:
    root, environment = _deployment_release(tmp_path)

    with pytest.raises(DeploymentConsistencyError, match="code image identity"):
        _run_deployment_preflight(root, environment, app_git_commit="2" * 40)


def test_deployment_preflight_rejects_artifact_root_identity_mismatch(
    tmp_path: Path,
) -> None:
    root, environment = _deployment_release(tmp_path)

    with pytest.raises(DeploymentConsistencyError, match="artifact release identity"):
        _run_deployment_preflight(
            root,
            environment,
            host_artifact_root="/opt/mirae-agent/releases/stale-release",
        )


def test_deployment_preflight_rejects_non_ready_derived_manifest(
    tmp_path: Path,
) -> None:
    root, environment = _deployment_release(tmp_path)
    _rewrite_semantic_manifest(root, status="BUILDING")

    with pytest.raises(DeploymentConsistencyError, match="status='BUILDING'"):
        _run_deployment_preflight(root, environment)


def test_deployment_preflight_rejects_missing_required_artifact(
    tmp_path: Path,
) -> None:
    root, environment = _deployment_release(tmp_path)
    (root / "data" / "semantic_search.json").unlink()

    with pytest.raises(DeploymentConsistencyError, match="No such file|does not exist"):
        _run_deployment_preflight(root, environment)


def test_deployment_health_requires_all_ready_signals() -> None:
    validate_health_payload({
        "status": "ok",
        "readiness_status": "READY",
        "semantic_index_readiness": "READY",
        "compatibility_status": "READY",
        "production_artifacts_readiness": "READY",
    })
    with pytest.raises(DeploymentConsistencyError, match="compatibility_status"):
        validate_health_payload({
            "status": "ok",
            "readiness_status": "READY",
            "semantic_index_readiness": "READY",
            "compatibility_status": "NOT_READY",
            "production_artifacts_readiness": "READY",
        })


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
    assert "AGENT_IMAGE_TAG: ${{ github.sha }}" in workflow
    assert "APP_GIT_COMMIT: ${{ github.sha }}" in workflow
    assert 'test "${IMAGE##*:}" = "$CODE_SHA"' in workflow
    assert ":latest" not in workflow


def test_naver_deploy_requires_bundle_checksum_and_health_before_promotion() -> None:
    script = Path("scripts/deploy_naver.sh").read_text()
    checksum = script.index("sha256sum -c")
    preflight = script.rindex("deployment_preflight \\")
    runtime_preflight = script.index("production runtime preflight passed", preflight)
    start = script.index("up -d --no-deps --force-recreate agent-api", preflight)
    ready = script.rindex("wait_until_ready \\")
    smoke = script.index("/answer")
    promote = script.index('ln -sfn "$code_release_dir" "$base/current"')
    assert checksum < preflight < runtime_preflight < start < ready < smoke < promote
    assert "/live" in script
    assert "/health" in script
    assert "trap deployment_failed ERR" in script
    assert 'environment_file="$base/.env"' in script
    assert 'release_environment_file="$code_release_dir/deployment.env"' in script
    assert 'incoming="$base/incoming/$artifact_release_id.tar"' in script
    assert 'artifact_dir="$release_base/$artifact_release_id"' in script
    assert 'test -f "$artifact_dir/release.json"' in script
    assert "--env-file \"$environment_file\"" in script
    assert "--env-file \"$release_env\"" in script
    assert "printf 'SEMANTIC_ARTIFACT_ROOT=%s\\n'" in script
    assert "docker inspect --format '{{.Config.Image}}'" in script
    assert ".Destination \"/var/lib/financial-semantic-agent\"" in script
    assert "semantic_index_readiness" in Path(
        "app/deployment/consistency.py"
    ).read_text()
    assert 'python3 "$app_dir/scripts/deployment_diagnostics.py"' in script
    diagnostics = Path("scripts/deployment_diagnostics.py").read_text()
    assert 'docker", "logs", "--tail", "80"' in diagnostics
    assert "[REDACTED]" in diagnostics
    assert '$artifact_release_id/artifacts' not in script
    assert '$previous_artifact_release_id/artifacts' not in script
    assert "tar -xf" not in script
    assert "up -d --no-deps --force-recreate agent-api" in script
    assert "/assets/app.js" in script
    assert "question=" in script
    assert "expected=" in script
    assert 'echo "rollback restored $previous_code_sha"' in script
    assert 'echo "rollback health verification failed"' in script


def test_naver_rollback_never_recreates_data_services() -> None:
    script = Path("scripts/deploy_naver.sh").read_text()
    rollback = script.split("rollback() {", 1)[1].split(
        "\ndeployment_failed() {", 1
    )[0]

    assert "up -d --no-deps --force-recreate agent-api" in rollback
    assert "up -d --no-deps --force-recreate frontend" in rollback
    assert "postgres" not in rollback
    assert "neo4j" not in rollback
    assert "--no-recreate" not in rollback


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
    assert 'release_base="$base/releases"' in script
    assert 'artifact_dir="$release_base/$artifact_release_id"' in script
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
