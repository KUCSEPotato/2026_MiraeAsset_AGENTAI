from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from app.external_data.holdings.contract import (
    DATA_CUTOFF_DATE,
    HoldingsContractError,
    require_cutoff_eligible,
    write_holdings,
)
from app.external_data.holdings.models import (
    ExternalHolding,
    HoldingValidationStatus,
    IdentityStatus,
    NumericStatus,
    ProductCategory,
    QuantityUnit,
    TemporalStatus,
    WeightScale,
    WeightUnit,
    deterministic_holding_id,
)
from app.external_data.holdings.normalize import (
    clean_optional_text,
    kodex_percent_to_proportion,
    parse_nonnegative_decimal,
)
from app.external_data.http import FetchResult, TrustedHttpClient
from app.external_data.manifest import NormalizedOutputEntry, SnapshotWorkspace
from app.external_data.models import (
    ContentType,
    CrawlFailure,
    ExternalSourceRecord,
    FailureStage,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


KODEX_PROVIDER = "Samsung Asset Management KODEX"
KODEX_PARSER_VERSION = "kodex-holdings-json-v1"
_F_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_KOREAN_TICKER = re.compile(r"\d{6}\Z")


class KodexSchemaError(HoldingsContractError):
    pass


class KodexHoldingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    risep: str | None
    totalCnt: str
    secNm: str
    evalA: str | None
    basrpRt: str | None
    applyQ: str | None
    itmNo: str | None
    curp: str | None
    ratio: str | None
    pdfType: str | None


class KodexPdfPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    gijunYMD: str
    totalCnt: int
    pdfExcelDownloadUrl: str
    nowCnt: int
    list: list[KodexHoldingItem]
    rcvTime: str | None = None

    @field_validator("gijunYMD")
    @classmethod
    def validate_date_text(cls, value: str) -> str:
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError("gijunYMD must use YYYYMMDD")
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> KodexPdfPayload:
        if self.totalCnt != len(self.list) or self.nowCnt != len(self.list):
            raise ValueError("KODEX list count does not match totalCnt/nowCnt")
        return self

    @property
    def effective_date(self) -> date:
        return date.fromisoformat(
            f"{self.gijunYMD[:4]}-{self.gijunYMD[4:6]}-{self.gijunYMD[6:]}"
        )


class KodexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pdf: KodexPdfPayload


@dataclass(frozen=True, slots=True)
class KodexProduct:
    source_id: str
    name: str | None = None
    ticker: str | None = None
    isin: str | None = None

    def validate(self) -> None:
        if not _F_ID.fullmatch(self.source_id):
            raise ValueError("KODEX product source_id has an unsafe format")
        if not any(clean_optional_text(value) for value in (self.name, self.ticker, self.isin)):
            raise ValueError("KODEX product requires at least one source identity field")


@dataclass(frozen=True, slots=True)
class KodexAdapterResult:
    fetch: FetchResult
    source_record: ExternalSourceRecord | None
    holdings: tuple[ExternalHolding, ...]
    normalized_output: NormalizedOutputEntry | None


class KodexHoldingsAdapter:
    def __init__(
        self, client: TrustedHttpClient, workspace: SnapshotWorkspace,
        *, cutoff_date: date = DATA_CUTOFF_DATE,
    ) -> None:
        if cutoff_date != DATA_CUTOFF_DATE:
            raise ValueError("KODEX evaluation adapter cutoff must be 2026-08-24")
        self._client = client
        self._workspace = workspace
        self._cutoff_date = cutoff_date

    @staticmethod
    def source_url(product_source_id: str, requested_date: date) -> str:
        if not _F_ID.fullmatch(product_source_id):
            raise ValueError("KODEX product source_id has an unsafe format")
        return (
            "https://www.samsungfund.com/api/v1/kodex/product-pdf/"
            f"{product_source_id}.do?gijunYMD={requested_date.strftime('%Y.%m.%d')}"
        )

    async def acquire(
        self, product: KodexProduct, *, requested_date: date = DATA_CUTOFF_DATE,
    ) -> KodexAdapterResult:
        product.validate()
        if requested_date > self._cutoff_date:
            raise ValueError("KODEX requested date cannot be after the evaluation cutoff")
        url = self.source_url(product.source_id, requested_date)
        self._workspace.add_source(KODEX_PROVIDER, url)
        fetch = await self._client.fetch(url)
        if fetch.content is None or fetch.content_hash is None:
            self._add_failure(
                fetch,
                FailureStage.ROBOTS
                if fetch.quality_status is QualityStatus.BLOCKED
                else FailureStage.FETCH,
            )
            return KodexAdapterResult(fetch, None, (), None)

        artifact = self._workspace.preserve_raw(
            category="holdings", content=fetch.content, suffix="json",
            normalized_url=fetch.normalized_url, content_type=ContentType.JSON.value,
        )
        source_record_id = deterministic_source_record_id(
            source_provider=KODEX_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            normalized_url=fetch.normalized_url,
            raw_content_hash=fetch.content_hash,
        )
        try:
            response = KodexResponse.model_validate_json(fetch.content)
            effective_date = response.pdf.effective_date
        except (ValidationError, ValueError) as exc:
            record = self._source_record(
                fetch, source_record_id, artifact.relative_path,
                effective_date=None, quality_status=QualityStatus.PARSE_FAILED,
            )
            self._workspace.write_source_records(category="holdings", records=[record])
            self._add_failure(fetch, FailureStage.PARSE, exc)
            raise KodexSchemaError("KODEX response schema validation failed") from exc

        if effective_date > self._cutoff_date:
            record = self._source_record(
                fetch, source_record_id, artifact.relative_path,
                effective_date=effective_date,
                quality_status=QualityStatus.VALIDATION_FAILED,
            )
            self._workspace.write_source_records(category="holdings", records=[record])
            error = HoldingsContractError(
                f"KODEX effective date {effective_date} is after cutoff {self._cutoff_date}"
            )
            self._add_failure(fetch, FailureStage.VALIDATE, error)
            require_cutoff_eligible(effective_date)
            raise AssertionError("unreachable")

        existing = self._workspace.get_source_record(source_record_id)
        record = existing or self._source_record(
            fetch, source_record_id, artifact.relative_path,
            effective_date=effective_date, quality_status=QualityStatus.VALID,
        )
        self._workspace.write_source_records(category="holdings", records=[record])
        try:
            holdings = tuple(self._normalize(response, product, record))
        except ValueError as exc:
            self._add_failure(fetch, FailureStage.VALIDATE, exc)
            raise KodexSchemaError(
                "KODEX holding values violate the approved normalization contract"
            ) from exc
        if not holdings:
            return KodexAdapterResult(fetch, record, (), None)
        try:
            output = write_holdings(
                self._workspace, holdings, source_record_id=record.source_record_id,
            )
        except HoldingsContractError as exc:
            self._add_failure(fetch, FailureStage.VALIDATE, exc)
            raise KodexSchemaError("KODEX semantic holding contract failed") from exc
        return KodexAdapterResult(fetch, record, holdings, output)

    def _normalize(
        self, response: KodexResponse, product: KodexProduct,
        record: ExternalSourceRecord,
    ) -> list[ExternalHolding]:
        rows: list[ExternalHolding] = []
        product_status = (
            IdentityStatus.VERIFIED_IDENTIFIER
            if clean_optional_text(product.isin) or clean_optional_text(product.ticker)
            else IdentityStatus.SOURCE_ID_ONLY
        )
        for item in response.pdf.list:
            name = clean_optional_text(item.secNm)
            if name is None:
                raise KodexSchemaError("KODEX constituent name cannot be blank")
            source_id = clean_optional_text(item.itmNo)
            ticker = source_id if source_id and _KOREAN_TICKER.fullmatch(source_id) else None
            non_security = name == "원화예금" or (source_id or "").startswith("KRD")
            constituent_status = (
                IdentityStatus.NON_SECURITY if non_security
                else IdentityStatus.SOURCE_ID_ONLY if source_id
                else IdentityStatus.NAME_ONLY
            )
            weight = kodex_percent_to_proportion(item.ratio)
            quantity = parse_nonnegative_decimal(item.applyQ, field="applyQ")
            market_value = parse_nonnegative_decimal(item.evalA, field="evalA")
            numeric_values = (weight, quantity, market_value)
            numeric_status = (
                NumericStatus.VALIDATED if all(value is not None for value in numeric_values)
                else NumericStatus.PARTIAL if any(value is not None for value in numeric_values)
                else NumericStatus.RAW_ONLY
            )
            overall_identity = constituent_status
            constituent_key = (
                f"ticker:KRX:{ticker}" if ticker else
                f"provider_security_id:{source_id}" if source_id else
                "name_sha256:" + hashlib.sha256(name.encode()).hexdigest()
            )
            row_id = deterministic_holding_id(
                source_provider=KODEX_PROVIDER,
                product_source_id=product.source_id,
                constituent_key=constituent_key,
                effective_date=response.pdf.effective_date,
            )
            rows.append(ExternalHolding(
                holding_record_id=row_id,
                product_category=ProductCategory.DOMESTIC_ETF,
                product_name_raw=clean_optional_text(product.name),
                product_ticker=clean_optional_text(product.ticker),
                product_isin=clean_optional_text(product.isin),
                product_source_id=product.source_id,
                constituent_name_raw=name,
                constituent_ticker=ticker,
                constituent_isin=None,
                constituent_source_id=source_id,
                weight_raw=clean_optional_text(item.ratio),
                weight_normalized=weight,
                weight_unit=WeightUnit.PERCENT_OF_NON_CASH_ASSETS if weight is not None else None,
                weight_scale=WeightScale.PERCENT_POINTS if weight is not None else None,
                quantity_raw=clean_optional_text(item.applyQ),
                quantity_normalized=quantity,
                quantity_unit=QuantityUnit.UNITS_PER_CREATION_UNIT if quantity is not None else None,
                market_value_raw=clean_optional_text(item.evalA),
                market_value_normalized=market_value,
                market_value_currency="KRW" if market_value is not None else None,
                rank=None,
                effective_date=response.pdf.effective_date,
                published_at=None,
                retrieved_at=record.retrieved_at,
                source_record_id=record.source_record_id,
                source_provider=record.source_provider,
                source_url=record.source_url,
                source_trust_tier=record.source_trust_tier,
                snapshot_id=record.snapshot_id,
                identity_status=overall_identity,
                product_identity_status=product_status,
                constituent_identity_status=constituent_status,
                numeric_status=numeric_status,
                temporal_status=TemporalStatus.EFFECTIVE_DATE_VERIFIED,
                validation_status=(
                    HoldingValidationStatus.VALID
                    if source_id is not None and numeric_status is NumericStatus.VALIDATED
                    else HoldingValidationStatus.PARTIAL
                ),
            ))
        return rows

    def _source_record(
        self, fetch: FetchResult, source_record_id: str, artifact_path: str,
        *, effective_date: date | None, quality_status: QualityStatus,
    ) -> ExternalSourceRecord:
        return ExternalSourceRecord(
            source_record_id=source_record_id,
            source_provider=KODEX_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            retrieved_at=fetch.retrieved_at,
            published_at=None,
            effective_date=effective_date,
            source_title="KODEX ETF Portfolio Deposit File",
            content_type=ContentType.JSON,
            http_status=fetch.status_code,
            raw_content_hash=fetch.content_hash or "",
            parser_version=KODEX_PARSER_VERSION,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=quality_status,
            raw_artifact_path=artifact_path,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            metadata={
                "from_http_cache": fetch.from_cache,
                "not_modified": fetch.not_modified,
                "fetch_attempts": fetch.attempts,
                "data_cutoff_date": self._cutoff_date.isoformat(),
                "product_source_id": fetch.normalized_url.split("/product-pdf/", 1)[-1]
                .split(".do", 1)[0],
            },
        )

    def _add_failure(
        self, fetch: FetchResult, stage: FailureStage,
        error: Exception | None = None,
    ) -> None:
        self._workspace.add_failure(CrawlFailure(
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            source_provider=KODEX_PROVIDER,
            failure_stage=stage,
            quality_status=(
                fetch.quality_status if error is None else
                QualityStatus.PARSE_FAILED if stage is FailureStage.PARSE else
                QualityStatus.VALIDATION_FAILED
            ),
            error_type=fetch.error_type or (type(error).__name__ if error else "FetchError"),
            error_message=fetch.error_message or str(error or "source acquisition failed"),
            retry_count=max(fetch.attempts - 1, 0),
        ))
