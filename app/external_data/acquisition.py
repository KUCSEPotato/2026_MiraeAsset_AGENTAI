from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.external_data.http import FetchResult, TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace
from app.external_data.models import (
    CrawlFailure,
    ExternalSourceRecord,
    FailureStage,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


@dataclass(frozen=True, slots=True)
class SourceRequest:
    provider: str
    source_type: SourceType
    trust_tier: SourceTrustTier
    url: str
    category: str
    parser_version: str = "raw-preservation-v1"
    published_at: datetime | None = None
    effective_date: date | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    fetch: FetchResult
    source_record: ExternalSourceRecord | None


class ExternalSourceAcquirer:
    def __init__(self, client: TrustedHttpClient, workspace: SnapshotWorkspace) -> None:
        self._client = client
        self._workspace = workspace

    async def acquire(self, request: SourceRequest) -> AcquisitionResult:
        self._workspace.add_source(request.provider, request.url)
        result = await self._client.fetch(request.url)
        if result.content is None or result.content_hash is None:
            self._workspace.add_failure(CrawlFailure(
                source_url=request.url,
                normalized_url=result.normalized_url,
                source_provider=request.provider,
                failure_stage=(
                    FailureStage.ROBOTS
                    if result.quality_status is QualityStatus.BLOCKED
                    else FailureStage.FETCH
                ),
                quality_status=result.quality_status,
                error_type=result.error_type or "FetchError",
                error_message=result.error_message or "source acquisition failed",
                retry_count=max(result.attempts - 1, 0),
            ))
            return AcquisitionResult(fetch=result, source_record=None)

        artifact = self._workspace.preserve_raw(
            category=request.category,
            content=result.content,
            suffix=_artifact_suffix(result, request.url),
            normalized_url=result.normalized_url,
            content_type=result.content_type.value,
        )
        record = ExternalSourceRecord(
            source_record_id=deterministic_source_record_id(
                source_provider=request.provider,
                source_type=request.source_type,
                normalized_url=result.normalized_url,
                raw_content_hash=result.content_hash,
            ),
            source_provider=request.provider,
            source_type=request.source_type,
            source_trust_tier=request.trust_tier,
            source_url=request.url,
            normalized_url=result.normalized_url,
            retrieved_at=result.retrieved_at,
            published_at=request.published_at,
            effective_date=request.effective_date,
            source_title=request.title,
            content_type=result.content_type,
            http_status=result.status_code,
            raw_content_hash=result.content_hash,
            parser_version=request.parser_version,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=QualityStatus.VALID,
            raw_artifact_path=artifact.relative_path,
            etag=result.etag,
            last_modified=result.last_modified,
            metadata={
                "from_http_cache": result.from_cache,
                "not_modified": result.not_modified,
                "fetch_attempts": result.attempts,
            },
        )
        self._workspace.write_source_records(category=request.category, records=[record])
        return AcquisitionResult(fetch=result, source_record=record)


def _artifact_suffix(result: FetchResult, url: str) -> str:
    by_type = {
        "HTML": "html", "JSON": "json", "CSV": "csv", "XLSX": "xlsx",
        "PDF": "pdf", "XML": "xml", "TEXT": "txt", "BINARY": "bin",
    }
    suffix = Path(urlsplit(url).path).suffix.lstrip(".")
    return suffix if suffix and len(suffix) <= 8 else by_type[result.content_type.value]
