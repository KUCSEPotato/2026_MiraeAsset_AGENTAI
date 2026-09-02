from __future__ import annotations

import json
from pathlib import Path

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


def _manifest(root: Path) -> dict:
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
        "schema_version": "m10.9-c3.2-production-artifacts-v1",
        "release_status": "READY",
        "deployment_version": "c3.2-test",
        "release_id": "c3.2-test",
        "git_commit": "1" * 40,
        "cutoff": "2026-08-24",
        **VERSIONS,
        "artifacts": artifacts,
    }


def test_production_manifest_requires_all_versions_and_checksums(tmp_path: Path) -> None:
    payload = _manifest(tmp_path)
    path = tmp_path / "production-artifacts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_and_verify_production_manifest(path, tmp_path, **VERSIONS)

    assert loaded.release_status == "READY"
    assert {item.role for item in loaded.artifacts} == REQUIRED_ARTIFACT_ROLES


def test_production_manifest_rejects_checksum_drift(tmp_path: Path) -> None:
    payload = _manifest(tmp_path)
    path = tmp_path / "production-artifacts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "kodex_holdings").write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch: kodex_holdings"):
        load_and_verify_production_manifest(path, tmp_path, **VERSIONS)


def test_production_manifest_rejects_version_drift(tmp_path: Path) -> None:
    payload = _manifest(tmp_path)
    payload["ontology_version"] = "stale"
    path = tmp_path / "production-artifacts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="versions are incompatible"):
        load_and_verify_production_manifest(path, tmp_path, **VERSIONS)


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
