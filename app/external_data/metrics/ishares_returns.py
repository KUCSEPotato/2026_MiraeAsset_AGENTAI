"""Official BlackRock/iShares published one-year NAV total returns."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.ishares_production import audit_pref02_foreign_etfs
from app.external_data.holdings.providers.ishares import ISharesProduct
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace
from app.external_data.metrics.models import (
    EXTERNAL_METRIC_OBSERVATION_SCHEMA,
    ExternalMetricObservation,
    deterministic_metric_observation_id,
)
from app.external_data.models import (
    ContentType,
    ExternalSourceRecord,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


ISHARES_RETURN_PROVIDER = "BlackRock iShares"
ISHARES_RETURN_OBSERVATION_DATE = date(2026, 7, 31)
ISHARES_RETURN_PARSER_VERSION = "ishares-published-return-json-v1"
ISHARES_RETURN_TRANSFORMER_VERSION = "m10.9-c3.0-ishares-return-1"
ISHARES_RETURN_SCOPE = "ISHARES_FOREIGN_ETF_ONE_YEAR_RETURN"
ISHARES_RETURN_DATASET_CODE = "ISHARES_US_PERFORMANCE"
ISHARES_RETURN_READY_TICKERS = frozenset({"EWY", "IYW", "SOXX"})
ISHARES_RETURN_SOURCE_SCHEMA = "external-ishares-return-result-v1"
_API = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v2/get-product-data"
)
_NAV_TOTAL_RETURN_METHOD = (
    "Total return represents changes to the NAV and accounts for distributions "
    "from the fund."
)


class ISharesReturnSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ISharesReturnAcquisition:
    product: ISharesProduct
    status: str
    source_record: ExternalSourceRecord | None = None
    observation: ExternalMetricObservation | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ISharesReturnProductionResult:
    first_results: tuple[ISharesReturnAcquisition, ...]
    second_results: tuple[ISharesReturnAcquisition, ...]
    first_semantic_checksum: str | None
    second_semantic_checksum: str | None
    rerun_performed: bool

    @property
    def idempotent(self) -> bool:
        return self.first_semantic_checksum == self.second_semantic_checksum

    @property
    def status_counts(self) -> Counter[str]:
        return Counter(item.status for item in self.second_results)

    @property
    def observations(self) -> tuple[ExternalMetricObservation, ...]:
        return tuple(
            item.observation for item in self.second_results
            if item.observation is not None
        )


def ishares_return_url(portfolio_id: str, observation_date: date) -> str:
    query = urlencode({
        "appSubType": "ISHARES",
        "appType": "PRODUCT_PAGE",
        "component": "performance.returns.average",
        "locale": "en_US",
        "portfolioId": portfolio_id,
        "targetSite": "us-ishares",
        "userType": "individual",
        "excludeContent": "false",
        "asOfDate": observation_date.strftime("%Y%m%d"),
        "includeConfig": "true",
    })
    return f"{_API}?{query}"


def parse_ishares_one_year_return(
    content: bytes, *, product: ISharesProduct, requested_date: date,
    source_record_id: str, source_url: str,
) -> ExternalMetricObservation:
    try:
        payload = json.loads(content)
        if str(payload["productId"]) != product.portfolio_id:
            raise ISharesReturnSchemaError("provider product ID mismatch")
        if payload["currencyCode"] != "USD":
            raise ISharesReturnSchemaError("unexpected iShares return currency")
        points = payload["componentsByNameMap"]["performance"][
            "containersByNameMap"
        ]["returns"]["subContainersByNameMap"]["average"]["dataPointsByNameMap"]
        actual_date = date.fromisoformat(
            str(points["asOfDate"]["value"])[0:4] + "-"
            + str(points["asOfDate"]["value"])[4:6] + "-"
            + str(points["asOfDate"]["value"])[6:8]
        )
        return_types = list(points["returnTypes"]["value"])
        values = list(points["oneYearAnnualized"]["value"])
        methods = list(points["returnTypes"]["infoBubble"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ISharesReturnSchemaError):
            raise
        raise ISharesReturnSchemaError("official return response schema mismatch") from exc
    if actual_date != requested_date:
        raise ISharesReturnSchemaError("official return observation date mismatch")
    if actual_date > DATA_CUTOFF_DATE:
        raise ISharesReturnSchemaError("official return observation is post-cutoff")
    if len(return_types) != len(values) or len(methods) != len(return_types):
        raise ISharesReturnSchemaError("return type/value alignment mismatch")
    if return_types.count("navSourced") != 1:
        raise ISharesReturnSchemaError("exactly one NAV total return is required")
    index = return_types.index("navSourced")
    if methods[index] != _NAV_TOTAL_RETURN_METHOD:
        raise ISharesReturnSchemaError("NAV total-return distribution semantics changed")
    raw_value = str(values[index])
    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ISharesReturnSchemaError("one-year return is not decimal") from exc
    if not value.is_finite():
        raise ISharesReturnSchemaError("one-year return must be finite")
    observation_id = deterministic_metric_observation_id(
        provider=ISHARES_RETURN_PROVIDER,
        product_source_id=product.source_id,
        metric_code="ONE_YEAR_RETURN",
        observation_end_date=actual_date,
        exact_period="1Y",
        return_basis="NAV_TOTAL_RETURN",
    )
    return ExternalMetricObservation(
        metric_observation_id=observation_id,
        source_record_id=source_record_id,
        source_provider=ISHARES_RETURN_PROVIDER,
        source_url=source_url,
        product_source_id=product.source_id,
        product_isin=product.isin,
        product_ticker=product.ticker,
        product_exchange=product.exchange,
        provider_product_id=product.portfolio_id,
        metric_code="ONE_YEAR_RETURN",
        raw_value=raw_value,
        numeric_value=value,
        unit="PERCENT",
        scale_basis="ISHARES_NAV_TOTAL_RETURN_PCT_V1",
        observation_end_date=actual_date,
        observation_start_date=None,
        exact_period="1Y",
        calculation_method="OFFICIAL_PUBLISHED_AVERAGE_ANNUAL_RETURN",
        return_basis="NAV_TOTAL_RETURN",
        distribution_treatment="INCLUDED",
        currency="USD",
        cutoff_valid=True,
        transformer_version=ISHARES_RETURN_TRANSFORMER_VERSION,
    )


class ISharesPublishedReturnAdapter:
    def __init__(self, client: TrustedHttpClient, workspace: SnapshotWorkspace) -> None:
        self._client = client
        self._workspace = workspace

    async def acquire(
        self, product: ISharesProduct, *, requested_date: date,
    ) -> ISharesReturnAcquisition:
        url = ishares_return_url(product.portfolio_id, requested_date)
        self._workspace.add_source(ISHARES_RETURN_PROVIDER, url)
        fetch = await self._client.fetch(url)
        if fetch.content is None or fetch.content_hash is None:
            return ISharesReturnAcquisition(
                product, "FETCH_FAILED", reason=fetch.error_message or "fetch failed"
            )
        artifact = self._workspace.preserve_raw(
            category="metrics", content=fetch.content, suffix="json",
            normalized_url=fetch.normalized_url, content_type=ContentType.JSON.value,
        )
        source_id = deterministic_source_record_id(
            source_provider=ISHARES_RETURN_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            normalized_url=fetch.normalized_url,
            raw_content_hash=fetch.content_hash,
        )
        source = ExternalSourceRecord(
            source_record_id=source_id,
            source_provider=ISHARES_RETURN_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            retrieved_at=fetch.retrieved_at,
            effective_date=requested_date,
            source_title="iShares Published Average Annual Performance",
            content_type=ContentType.JSON,
            http_status=fetch.status_code,
            raw_content_hash=fetch.content_hash,
            parser_version=ISHARES_RETURN_PARSER_VERSION,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=QualityStatus.VALID,
            raw_artifact_path=artifact.relative_path,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            metadata={
                "portfolio_id": product.portfolio_id,
                "product_isin": product.isin,
                "product_ticker": product.ticker,
                "product_exchange_mic": product.exchange,
                "metric_code": "ONE_YEAR_RETURN",
                "return_basis": "NAV_TOTAL_RETURN",
                "distribution_treatment": "INCLUDED",
                "observation_date_contract": "official selected asOfDate",
            },
        )
        try:
            observation = parse_ishares_one_year_return(
                fetch.content, product=product, requested_date=requested_date,
                source_record_id=source_id, source_url=fetch.requested_url,
            )
        except ISharesReturnSchemaError as exc:
            return ISharesReturnAcquisition(product, "PARSE_FAILED", source, reason=str(exc))
        self._workspace.write_source_records(category="metrics", records=[source])
        self._workspace.write_normalized_jsonl(
            category="metrics", filename="metric_observations.jsonl",
            schema_version=EXTERNAL_METRIC_OBSERVATION_SCHEMA,
            canonical_rows=[observation.canonical_json()],
        )
        return ISharesReturnAcquisition(product, "SUCCESS", source, observation)


async def run_ishares_return_crawl(
    client: TrustedHttpClient,
    workspace: SnapshotWorkspace,
    *,
    pref02_data: Path,
    requested_date: date = ISHARES_RETURN_OBSERVATION_DATE,
    selected_tickers: frozenset[str] = ISHARES_RETURN_READY_TICKERS,
    verify_rerun: bool = True,
) -> ISharesReturnProductionResult:
    if requested_date > DATA_CUTOFF_DATE:
        raise ValueError("iShares return observation date cannot be post-cutoff")
    audit = audit_pref02_foreign_etfs(pref02_data)
    selected = {value.upper() for value in selected_tickers}
    if not selected or not selected.issubset(ISHARES_RETURN_READY_TICKERS):
        raise ValueError("selected return scope must be within the reviewed iShares READY set")
    products = tuple(
        item for item in audit.reviewed_products if item.ticker in selected
    )
    if {item.ticker for item in products} != selected:
        raise ValueError("PREF02 cannot deterministically resolve selected iShares products")
    adapter = ISharesPublishedReturnAdapter(client, workspace)
    first = await _crawl_pass(adapter, products, requested_date)
    first_checksum = _semantic_checksum(first)
    second = first
    if verify_rerun:
        second = await _crawl_pass(adapter, products, requested_date)
    second_checksum = _semantic_checksum(second)
    workspace.write_normalized_jsonl(
        category="metrics", filename="metric_observations.jsonl",
        schema_version=EXTERNAL_METRIC_OBSERVATION_SCHEMA,
        canonical_rows=[
            item.observation.canonical_json() for item in second
            if item.observation is not None
        ],
    )
    workspace.write_normalized_jsonl(
        category="metrics", filename="crawl_results.jsonl",
        schema_version=ISHARES_RETURN_SOURCE_SCHEMA,
        canonical_rows=[json.dumps({
            "portfolio_id": item.product.portfolio_id,
            "product_ticker": item.product.ticker,
            "product_isin": item.product.isin,
            "status": item.status,
            "reason": item.reason,
            "metric_observation_id": (
                item.observation.metric_observation_id if item.observation else None
            ),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in second],
    )
    return ISharesReturnProductionResult(
        first, second, first_checksum, second_checksum, verify_rerun
    )


async def _crawl_pass(adapter, products, requested_date):
    return tuple([
        await adapter.acquire(item, requested_date=requested_date)
        for item in sorted(products, key=lambda value: value.ticker)
    ])


def _semantic_checksum(rows: tuple[ISharesReturnAcquisition, ...]) -> str | None:
    semantic = sorted(
        item.observation.semantic_json() for item in rows
        if item.observation is not None
    )
    if not semantic:
        return None
    return hashlib.sha256(("\n".join(semantic) + "\n").encode()).hexdigest()
