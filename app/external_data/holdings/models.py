from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.external_data.models import EXTERNAL_HOLDINGS_SCHEMA, SourceTrustTier


class ProductCategory(StrEnum):
    DOMESTIC_ETF = "DOMESTIC_ETF"


class IdentityStatus(StrEnum):
    VERIFIED_IDENTIFIER = "VERIFIED_IDENTIFIER"
    SOURCE_ID_ONLY = "SOURCE_ID_ONLY"
    NAME_ONLY = "NAME_ONLY"
    NON_SECURITY = "NON_SECURITY"
    UNRESOLVED = "UNRESOLVED"


class WeightUnit(StrEnum):
    PERCENT_OF_NON_CASH_ASSETS = "PERCENT_OF_NON_CASH_ASSETS"


class WeightScale(StrEnum):
    PERCENT_POINTS = "PERCENT_POINTS"


class QuantityUnit(StrEnum):
    UNITS_PER_CREATION_UNIT = "UNITS_PER_CREATION_UNIT"


class NumericStatus(StrEnum):
    VALIDATED = "VALIDATED"
    PARTIAL = "PARTIAL"
    RAW_ONLY = "RAW_ONLY"


class TemporalStatus(StrEnum):
    EFFECTIVE_DATE_VERIFIED = "EFFECTIVE_DATE_VERIFIED"
    CUTOFF_UNVERIFIED = "CUTOFF_UNVERIFIED"


class HoldingValidationStatus(StrEnum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"


class ExternalHolding(BaseModel):
    """Provider-level holding fact; canonical Agent identifiers are forbidden."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_HOLDINGS_SCHEMA
    holding_record_id: str

    product_category: ProductCategory = ProductCategory.DOMESTIC_ETF
    product_name_raw: str | None = None
    product_ticker: str | None = None
    product_isin: str | None = None
    product_source_id: str

    constituent_name_raw: str
    constituent_ticker: str | None = None
    constituent_isin: str | None = None
    constituent_source_id: str | None = None

    weight_raw: str | None = None
    weight_normalized: Decimal | None = None
    weight_unit: WeightUnit | None = None
    weight_scale: WeightScale | None = None
    quantity_raw: str | None = None
    quantity_normalized: Decimal | None = None
    quantity_unit: QuantityUnit | None = None
    market_value_raw: str | None = None
    market_value_normalized: Decimal | None = None
    market_value_currency: str | None = None
    rank: int | None = Field(default=None, ge=1)

    effective_date: date
    published_at: datetime | None = None
    retrieved_at: datetime

    source_record_id: str
    source_provider: str
    source_url: str
    source_trust_tier: SourceTrustTier
    snapshot_id: str

    identity_status: IdentityStatus
    product_identity_status: IdentityStatus
    constituent_identity_status: IdentityStatus
    numeric_status: NumericStatus
    temporal_status: TemporalStatus
    validation_status: HoldingValidationStatus

    @field_validator("retrieved_at")
    @classmethod
    def require_aware_retrieval(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("published_at")
    @classmethod
    def require_aware_publication(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @field_validator("source_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            raise ValueError("source_url must be an absolute HTTP(S) URL")
        return value

    @field_validator("weight_normalized")
    @classmethod
    def validate_weight(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not Decimal("0") <= value <= Decimal("1"):
            raise ValueError("weight_normalized must be a proportion from 0 through 1")
        return value

    @field_validator("quantity_normalized", "market_value_normalized")
    @classmethod
    def validate_nonnegative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("normalized quantities and market values cannot be negative")
        return value

    @field_validator("market_value_currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 3 or not value.isalpha()):
            raise ValueError("market_value_currency must be a three-letter currency code")
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def validate_semantic_pairs(self) -> ExternalHolding:
        if not self.product_source_id.strip():
            raise ValueError("product_source_id cannot be blank")
        if not self.constituent_name_raw.strip():
            raise ValueError("constituent_name_raw cannot be blank")
        if self.weight_normalized is not None and (
            self.weight_unit is None or self.weight_scale is None
        ):
            raise ValueError("normalized weight requires explicit unit and scale")
        if self.quantity_normalized is not None and self.quantity_unit is None:
            raise ValueError("normalized quantity requires an explicit unit")
        if self.market_value_normalized is not None and self.market_value_currency is None:
            raise ValueError("normalized market value requires an explicit currency")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def deterministic_holding_id(
    *, source_provider: str, product_source_id: str,
    constituent_key: str, effective_date: date, source_record_id: str,
) -> str:
    payload = "|".join((
        source_provider,
        product_source_id,
        constituent_key,
        effective_date.isoformat(),
        source_record_id,
    ))
    return "holding_" + hashlib.sha256(payload.encode()).hexdigest()
