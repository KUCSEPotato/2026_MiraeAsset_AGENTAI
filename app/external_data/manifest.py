from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.external_data.models import CrawlFailure, ExternalSourceRecord, SourceQualityReport


MANIFEST_SCHEMA_VERSION = "external-snapshot-manifest-v1"


class SnapshotStatus(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class ArtifactManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    sha256: str
    byte_count: int
    normalized_url: str | None = None
    content_type: str | None = None


class NormalizedOutputEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    relative_path: str
    sha256: str
    row_count: int


class ExternalSnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MANIFEST_SCHEMA_VERSION
    snapshot_id: str
    snapshot_date: date
    data_cutoff_date: date | None = None
    created_at: datetime
    completed_at: datetime | None = None
    crawler_version: str
    parser_versions: dict[str, str] = Field(default_factory=dict)
    status: SnapshotStatus = SnapshotStatus.BUILDING
    sources: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    raw_artifacts: list[ArtifactManifestEntry] = Field(default_factory=list)
    raw_file_count: int = 0
    normalized_outputs: list[NormalizedOutputEntry] = Field(default_factory=list)
    source_record_count: int = 0
    normalized_row_counts: dict[str, int] = Field(default_factory=dict)
    failed_source_count: int = 0
    failures: list[CrawlFailure] = Field(default_factory=list)
    source_quality_reports: list[SourceQualityReport] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)


class SnapshotWorkspace:
    """Append-only snapshot writer. Existing snapshot directories are never reused."""

    def __init__(
        self, root: Path, *, snapshot_id: str, snapshot_date: date,
        crawler_version: str, data_cutoff_date: date | None = None,
    ) -> None:
        self.root = root
        self.snapshot_id = snapshot_id
        self.snapshot_date = snapshot_date
        self.path = root / "snapshots" / snapshot_date.isoformat() / snapshot_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.manifest = ExternalSnapshotManifest(
            snapshot_id=snapshot_id,
            snapshot_date=snapshot_date,
            data_cutoff_date=data_cutoff_date,
            created_at=datetime.now(UTC),
            crawler_version=crawler_version,
        )
        self._artifact_keys: set[tuple[str | None, str]] = set()
        self._record_ids: set[str] = set()
        self._records_by_category: dict[str, dict[str, ExternalSourceRecord]] = {}
        self._write_manifest()

    def raw_directory(self, category: str) -> Path:
        return self._safe_category_directory(category, "raw")

    def normalized_directory(self, category: str) -> Path:
        return self._safe_category_directory(category, "normalized")

    def preserve_raw(
        self, *, category: str, content: bytes, suffix: str,
        normalized_url: str | None = None, content_type: str | None = None,
    ) -> ArtifactManifestEntry:
        digest = hashlib.sha256(content).hexdigest()
        dedup_key = (normalized_url, digest)
        if dedup_key in self._artifact_keys:
            return next(
                item for item in self.manifest.raw_artifacts
                if item.normalized_url == normalized_url and item.sha256 == digest
            )
        safe_suffix = suffix.casefold().lstrip(".")
        if not safe_suffix.isalnum() or len(safe_suffix) > 8:
            safe_suffix = "bin"
        target = self.raw_directory(category) / f"{digest}.{safe_suffix}"
        if not target.exists():
            object_path = self.root / "objects" / digest[:2] / digest
            if not object_path.exists():
                _atomic_write_bytes(object_path, content)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(object_path, target)
            except OSError:
                _atomic_write_bytes(target, content)
        entry = ArtifactManifestEntry(
            relative_path=target.relative_to(self.path).as_posix(),
            sha256=digest,
            byte_count=len(content),
            normalized_url=normalized_url,
            content_type=content_type,
        )
        self.manifest.raw_artifacts.append(entry)
        self.manifest.raw_file_count = len(self.manifest.raw_artifacts)
        self._artifact_keys.add(dedup_key)
        self._write_manifest()
        return entry

    def write_source_records(
        self, *, category: str, records: Iterable[ExternalSourceRecord],
    ) -> NormalizedOutputEntry:
        category_records = self._records_by_category.setdefault(category, {})
        for record in records:
            if record.snapshot_id != self.snapshot_id:
                raise ValueError("source record snapshot_id does not match workspace")
            if record.source_record_id in self._record_ids:
                continue
            self._record_ids.add(record.source_record_id)
            category_records[record.source_record_id] = record
        accepted = sorted(category_records.values(), key=lambda item: item.source_record_id)
        versions = {item.parser_version for item in accepted}
        if len(versions) > 1:
            raise ValueError("one category cannot mix parser versions in a snapshot")
        if versions:
            self.manifest.parser_versions[category] = next(iter(versions))
        payload = "".join(item.canonical_json() + "\n" for item in accepted).encode()
        target = self.normalized_directory(category) / "source_records.jsonl"
        _atomic_write_bytes(target, payload)
        entry = NormalizedOutputEntry(
            schema_version="external-source-record-v1",
            relative_path=target.relative_to(self.path).as_posix(),
            sha256=hashlib.sha256(payload).hexdigest(),
            row_count=len(accepted),
        )
        self._replace_normalized_entry(entry)
        self.manifest.source_record_count = len(self._record_ids)
        self._write_manifest()
        return entry

    def write_normalized_jsonl(
        self, *, category: str, filename: str, schema_version: str,
        canonical_rows: Iterable[str],
    ) -> NormalizedOutputEntry:
        """Write deterministic domain rows and register them in the manifest."""

        if Path(filename).name != filename or not filename.endswith(".jsonl"):
            raise ValueError("normalized filename must be a plain .jsonl filename")
        rows = sorted(set(canonical_rows))
        payload = ("".join(row.rstrip("\n") + "\n" for row in rows)).encode()
        target = self.normalized_directory(category) / filename
        _atomic_write_bytes(target, payload)
        entry = NormalizedOutputEntry(
            schema_version=schema_version,
            relative_path=target.relative_to(self.path).as_posix(),
            sha256=hashlib.sha256(payload).hexdigest(),
            row_count=len(rows),
        )
        self._replace_normalized_entry(entry)
        self._write_manifest()
        return entry

    def add_source(self, provider: str, url: str) -> None:
        if provider not in self.manifest.sources:
            self.manifest.sources.append(provider)
        if url not in self.manifest.source_urls:
            self.manifest.source_urls.append(url)
        self._write_manifest()

    def get_source_record(self, source_record_id: str) -> ExternalSourceRecord | None:
        for records in self._records_by_category.values():
            if source_record_id in records:
                return records[source_record_id]
        return None

    def add_failure(self, failure: CrawlFailure) -> None:
        self.manifest.failures.append(failure)
        self.manifest.failed_source_count = len(self.manifest.failures)
        self._write_manifest()

    def finalize(
        self, status: SnapshotStatus, *, validation: dict[str, Any],
        quality_reports: list[SourceQualityReport] | None = None,
    ) -> ExternalSnapshotManifest:
        if status is SnapshotStatus.BUILDING:
            raise ValueError("final snapshot status cannot be BUILDING")
        self.manifest.status = status
        self.manifest.completed_at = datetime.now(UTC)
        self.manifest.validation = validation
        self.manifest.source_quality_reports = quality_reports or []
        counts: dict[str, int] = {}
        for entry in self.manifest.normalized_outputs:
            counts[entry.schema_version] = counts.get(entry.schema_version, 0) + entry.row_count
        self.manifest.normalized_row_counts = counts
        self._write_manifest()
        return self.manifest

    def _replace_normalized_entry(self, entry: NormalizedOutputEntry) -> None:
        self.manifest.normalized_outputs = [
            item for item in self.manifest.normalized_outputs
            if item.relative_path != entry.relative_path
        ]
        self.manifest.normalized_outputs.append(entry)

    def _safe_category_directory(self, category: str, leaf: str) -> Path:
        if not category.replace("_", "").replace("-", "").isalnum():
            raise ValueError("category must contain only letters, digits, '_' or '-'")
        target = self.path / category / leaf
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_manifest(self) -> None:
        payload = self.manifest.model_dump_json(indent=2).encode() + b"\n"
        _atomic_write_bytes(self.path / "manifest.json", payload)


def load_snapshot_manifest(
    root: Path, *, snapshot_date: date, snapshot_id: str,
) -> ExternalSnapshotManifest | None:
    path = root / "snapshots" / snapshot_date.isoformat() / snapshot_id / "manifest.json"
    if not path.is_file():
        return None
    return ExternalSnapshotManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
