from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


EXTERNAL_SOURCE_RECORD_SCHEMA = "external-source-record-v1"
EXTERNAL_HOLDINGS_SCHEMA = "external-holdings-v1"


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


class ExternalSourceRecord(BaseModel):
    """Immutable source-level evidence; canonical identifiers are forbidden."""

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
    etag: str | None = None
    last_modified: str | None = None
    raw_content_hash: str
    parser_version: str
    crawler_version: str
    snapshot_id: str
    quality_status: QualityStatus
    raw_artifact_path: str
    normalized_url: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw_content_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        value = value.casefold()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("raw_content_hash must be a SHA-256 hex digest")
        return value

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("external timestamps must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @field_validator("source_url", "normalized_url")
    @classmethod
    def absolute_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            raise ValueError("source URLs must be absolute HTTP(S) URLs")
        return value

    @field_validator("raw_artifact_path")
    @classmethod
    def relative_artifact(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("raw_artifact_path must be snapshot-relative")
        return value

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deterministic_source_record_id(*, source_provider: str, source_type: SourceType,
                                   normalized_url: str, raw_content_hash: str) -> str:
    value = "|".join((source_provider, source_type.value, normalized_url, raw_content_hash))
    return "extrec_" + hashlib.sha256(value.encode()).hexdigest()
