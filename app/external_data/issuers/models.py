from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator


EXTERNAL_SECURITY_ISSUER_SCHEMA = "external-security-issuer-v1"


class IssuerIdentityStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    UNRESOLVED = "UNRESOLVED"


class ExternalSecurityIssuerRecord(BaseModel):
    """Source-grain issuer evidence; canonical IDs are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_SECURITY_ISSUER_SCHEMA
    issuer_record_id: str

    security_name_raw: str
    security_ticker: str
    security_isin: str | None = None
    security_market: str
    security_source_id: str

    issuer_name_raw: str
    issuer_source_id: str
    official_corporate_id: str | None = None
    dart_corp_code: str | None = None
    registration_identifier: str | None = None

    relation_type: str = "SECURITY_ISSUED_BY"
    effective_date: date

    source_provider: str
    source_url: str
    source_record_id: str
    retrieved_at: datetime
    published_at: datetime | None = None
    snapshot_id: str
    trust_tier: int = 1

    security_identity_status: IssuerIdentityStatus
    issuer_identity_status: IssuerIdentityStatus
    relation_validation_status: IssuerIdentityStatus

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("issuer provenance timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("relation_type")
    @classmethod
    def require_security_issuer_relation(cls, value: str) -> str:
        if value != "SECURITY_ISSUED_BY":
            raise ValueError("source issuer records may only assert SECURITY_ISSUED_BY")
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )


def deterministic_issuer_record_id(
    *, provider: str, snapshot_id: str, market: str, ticker: str,
    issuer_source_id: str,
) -> str:
    payload = "|".join(
        (provider, snapshot_id, market, ticker, issuer_source_id)
    )
    return "issuerrec_" + hashlib.sha256(payload.encode()).hexdigest()
