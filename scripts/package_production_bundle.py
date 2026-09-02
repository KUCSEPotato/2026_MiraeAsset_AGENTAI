"""Validate and package an immutable production artifact bundle."""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.deployment.artifacts import (
    ProductionArtifactManifest,
    ProductionReleaseManifest,
    artifact_checksum,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--git-commit", required=True)
    args = parser.parse_args()

    root = args.bundle_root.resolve(strict=True)
    manifest = ProductionArtifactManifest.model_validate_json(
        args.manifest.read_text(encoding="utf-8")
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    members = {args.manifest.resolve()}
    for artifact in manifest.artifacts:
        target = (root / artifact.relative_path).resolve(strict=True)
        if not target.is_relative_to(root):
            raise SystemExit(f"artifact escapes bundle root: {artifact.role}")
        if target.is_dir():
            members.update(item for item in target.rglob("*") if item.is_file())
        else:
            members.add(target)
        if artifact_checksum(target) != artifact.sha256:
            raise SystemExit(f"artifact checksum mismatch: {artifact.role}")

    release = ProductionReleaseManifest(
        schema_version="m10.9-c3.3-release-v1",
        release_id=args.release_id,
        git_commit=args.git_commit,
        artifact_manifest=manifest,
    )
    release_bytes = (
        release.model_dump_json(indent=2).encode("utf-8") + b"\n"
    )

    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for source in sorted(members, key=lambda item: item.as_posix()):
            arcname = (
                "manifests/production-artifacts.json"
                if source == args.manifest.resolve()
                else source.relative_to(root).as_posix()
            )
            info = archive.gettarinfo(str(source), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with source.open("rb") as stream:
                archive.addfile(info, stream)
        release_info = tarfile.TarInfo("release.json")
        release_info.size = len(release_bytes)
        release_info.uid = release_info.gid = 0
        release_info.uname = release_info.gname = ""
        release_info.mtime = 0
        archive.addfile(release_info, BytesIO(release_bytes))

    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{checksum}  {output.name}\n", encoding="ascii"
    )
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
