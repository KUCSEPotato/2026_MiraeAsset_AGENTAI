from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from typing import Any
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


EXTERNAL_SOURCE_RECORD_SCHEMA = "external-source-record-v1"
EXTERNAL_HOLDINGS_SCHEMA = "external-holdings-v1"
EXTERNAL_CORPORATE_SCHEMA = "external-corporate-v1"
EXTERNAL_DOCUMENT_SCHEMA = "external-document-v1"


class SourceTrustTier(IntEnum):
    AUTHORITATIVE = 1
    TRUSTED_FINANCIAL = 2
    SUPPORTING_WEB = 3


class SourceType(StrEnum):
    ASSET_MANAGER = "ASSET_MANAGER"
    EXCHANGE = "EXCHANGE"
    REGULATORY_FILING = "REGULATORY_FILING"
    CORPORATE_DISCLOSURE = "CORPORATE_DISCLOSURE"
    FINANCIAL_DATA_PROVIDER = "FINANCIAL_DATA_PROVIDER"
    NEWS = "NEWS"
    GENERAL_WEB = "GENERAL_WEB"


class ContentType(StrEnum):
    HTML = "HTML"
    JSON = "JSON"
    CSV = "CSV"
    XLSX = "XLSX"
    PDF = "PDF"
    XML = "XML"
    TEXT = "TEXT"
    BINARY = "BINARY"


class QualityStatus(StrEnum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FETCH_FAILED = "FETCH_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class FailureStage(StrEnum):
    ROBOTS = "ROBOTS"
    FETCH = "FETCH"
    STORE = "STORE"
    PARSE = "PARSE"
    VALIDATE = "VALIDATE"


class ExternalSourceRecord(BaseModel):
    """Source-level evidence only; it intentionally has no canonical Agent ID."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_SOURCE_RECORD_SCHEMA
    source_record_id: str
    source_provider: str
    source_type: SourceType
    source_trust_tier: SourceTrustTier
    source_url: str
    retrieved_at: datetime
    published_at: datetime | None = None
    effective_date: date | None = None
    source_title: str | None = None
    content_type: ContentType
    http_status: int | None = None
    raw_content_hash: str
    parser_version: str
    crawler_version: str
    snapshot_id: str
    quality_status: QualityStatus
    raw_artifact_path: str
    normalized_url: str
    etag: str | None = None
    last_modified: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw_content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        lowered = value.casefold()
        if len(lowered) != 64 or any(character not in "0123456789abcdef" for character in lowered):
            raise ValueError("raw_content_hash must be a SHA-256 hex digest")
        return lowered

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_retrieval_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("published_at")
    @classmethod
    def require_aware_publication_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("published_at must be timezone-aware when present")
        return value.astimezone(UTC) if value is not None else None

    @field_validator("source_url", "normalized_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            raise ValueError("source URLs must be absolute HTTP(S) URLs")
        return value

    @field_validator("raw_artifact_path")
    @classmethod
    def require_relative_artifact_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("raw_artifact_path must be snapshot-relative")
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class CrawlFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str
    normalized_url: str
    source_provider: str
    failure_stage: FailureStage
    quality_status: QualityStatus
    error_type: str
    error_message: str
    retry_count: int = 0
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SourceQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    trust_tier: SourceTrustTier
    access_method: str
    data_types: list[ContentType]
    refresh_behavior: str
    identity_fields_available: list[str] = Field(default_factory=list)
    timestamps_available: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    terms_and_access_constraints: list[str] = Field(default_factory=list)
    attempted_sources: int = 0
    successful_sources: int = 0
    failed_sources: int = 0

    @computed_field
    @property
    def failure_rate(self) -> float:
        return self.failed_sources / self.attempted_sources if self.attempted_sources else 0.0


def deterministic_source_record_id(
    *, source_provider: str, source_type: SourceType, normalized_url: str,
    raw_content_hash: str,
) -> str:
    payload = "|".join((source_provider, source_type.value, normalized_url, raw_content_hash))
    return "extrec_" + hashlib.sha256(payload.encode()).hexdigest()
