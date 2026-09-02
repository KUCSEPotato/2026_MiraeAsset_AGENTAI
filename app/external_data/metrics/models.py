from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


EXTERNAL_METRIC_OBSERVATION_SCHEMA = "external-metric-observation-v1"


class ExternalMetricObservation(BaseModel):
    """Source-grain metric observation with no canonical entity identifier."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_METRIC_OBSERVATION_SCHEMA
    metric_observation_id: str
    source_record_id: str
    source_provider: str
    source_url: str
    product_source_id: str
    product_isin: str
    product_ticker: str
    product_exchange: str
    provider_product_id: str
    metric_code: Literal["ONE_YEAR_RETURN"]
    raw_value: str
    numeric_value: Decimal
    unit: Literal["PERCENT"]
    scale_basis: Literal["ISHARES_NAV_TOTAL_RETURN_PCT_V1"]
    observation_end_date: date
    observation_start_date: date | None = None
    exact_period: Literal["1Y"]
    calculation_method: Literal["OFFICIAL_PUBLISHED_AVERAGE_ANNUAL_RETURN"]
    return_basis: Literal["NAV_TOTAL_RETURN"]
    distribution_treatment: Literal["INCLUDED"]
    currency: str
    cutoff_valid: bool
    transformer_version: str

    @field_validator("numeric_value")
    @classmethod
    def require_finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("metric value must be finite")
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )

    def semantic_json(self) -> str:
        """Stable metric content independent of retrieval/evidence metadata."""

        payload = self.model_dump(mode="json", exclude={"source_record_id", "source_url"})
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )


def deterministic_metric_observation_id(
    *, provider: str, product_source_id: str, metric_code: str,
    observation_end_date: date, exact_period: str, return_basis: str,
) -> str:
    payload = "|".join((
        provider, product_source_id, metric_code, observation_end_date.isoformat(),
        exact_period, return_basis,
    ))
    return "extmetric_" + hashlib.sha256(payload.encode()).hexdigest()
