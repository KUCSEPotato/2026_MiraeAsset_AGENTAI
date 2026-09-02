"""Validate and package an immutable production artifact bundle."""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.deployment.artifacts import ProductionArtifactManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
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

    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for source in sorted(members, key=lambda item: item.as_posix()):
            if source == args.manifest.resolve():
                arcname = "release.json"
            else:
                arcname = source.relative_to(root).as_posix()
            info = archive.gettarinfo(str(source), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with source.open("rb") as stream:
                archive.addfile(info, stream)

    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{checksum}  {output.name}\n", encoding="ascii"
    )
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
