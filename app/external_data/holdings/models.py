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
    """Provider-level holding fact; it intentionally has no canonical IDs."""

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

    @field_validator("retrieved_at", "published_at")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("external timestamps must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None

    @field_validator("source_url")
    @classmethod
    def absolute_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
            raise ValueError("source_url must be absolute HTTP(S)")
        return value

    @model_validator(mode="after")
    def semantic_pairs(self) -> "ExternalHolding":
        if not self.product_source_id.strip() or not self.constituent_name_raw.strip():
            raise ValueError("product source ID and constituent name are required")
        if self.weight_normalized is not None:
            if not Decimal("0") <= self.weight_normalized <= Decimal("1"):
                raise ValueError("weight_normalized must be a proportion")
            if self.weight_unit is None or self.weight_scale is None:
                raise ValueError("normalized weight requires unit and scale")
        if self.quantity_normalized is not None and self.quantity_unit is None:
            raise ValueError("normalized quantity requires unit")
        if self.quantity_normalized is not None and self.quantity_normalized < 0:
            raise ValueError("quantity_normalized must be non-negative")
        if self.market_value_normalized is not None and self.market_value_currency is None:
            raise ValueError("normalized market value requires currency")
        if self.market_value_normalized is not None and self.market_value_normalized < 0:
            raise ValueError("market_value_normalized must be non-negative")
        if self.market_value_currency is not None:
            currency = self.market_value_currency.strip().upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValueError("market_value_currency must be an ISO-style three-letter code")
            self.market_value_currency = currency
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deterministic_holding_id(*, source_provider: str, product_source_id: str,
                             constituent_key: str, effective_date: date,
                             source_record_id: str) -> str:
    value = "|".join((source_provider, product_source_id, constituent_key,
                      effective_date.isoformat(), source_record_id))
    return "holding_" + hashlib.sha256(value.encode()).hexdigest()
