"""Official TIGER historical portfolio adapter.

The official ``pdfListAjax.ajax`` endpoint returns the portfolio deposit file
for the exact ``fixDate`` supplied by the caller.  Its table columns are:
security code, name, units per creation unit, evaluated KRW value, NAV weight
percent, and one-week return.  The final return column is intentionally not
part of the Holdings contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode

from app.external_data.holdings.contract import DATA_CUTOFF_DATE, HoldingsContractError, write_holdings
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
from app.external_data.holdings.normalize import clean_optional_text, parse_nonnegative_decimal
from app.external_data.http import FetchResult, TrustedHttpClient
from app.external_data.manifest import NormalizedOutputEntry, SnapshotWorkspace
from app.external_data.models import (
    ContentType,
    ExternalSourceRecord,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


TIGER_PROVIDER = "Mirae Asset Management TIGER"
TIGER_PARSER_VERSION = "tiger-holdings-html-v1"
TIGER_ENDPOINT = (
    "https://investments.miraeasset.com/"
    "tigeretf/ko/product/search/detail/pdfListAjax.ajax"
)
_ISIN = re.compile(r"KR[A-Z0-9]{10}\Z")
_KRX_TICKER = re.compile(r"[A-Z0-9]{6}\Z")


class TigerSchemaError(HoldingsContractError):
    pass


@dataclass(frozen=True, slots=True)
class TigerProduct:
    source_id: str
    name: str
    ticker: str
    isin: str
    exchange: str = "KRX"

    def validate(self) -> None:
        if not _ISIN.fullmatch(self.isin) or self.source_id != self.isin:
            raise ValueError("TIGER source product ID must be its validated Korean ISIN")
        if not _KRX_TICKER.fullmatch(self.ticker):
            raise ValueError("TIGER product ticker must be a six-character KRX ticker")
        if self.exchange != "KRX" or not self.name.strip():
            raise ValueError("TIGER product requires a KRX exchange and source name")


@dataclass(frozen=True, slots=True)
class TigerAdapterResult:
    fetch: FetchResult
    source_record: ExternalSourceRecord | None
    holdings: tuple[ExternalHolding, ...]
    normalized_output: NormalizedOutputEntry | None
    expected_row_count: int | None = None


class _TigerTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.totals: list[int] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            values = dict(attrs)
            total = values.get("data-tot-cnt")
            self._row = []
            if total is not None and total.isdigit():
                self.totals.append(int(total))
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def parse_tiger_holdings_html(content: bytes) -> tuple[tuple[str, str, str, str, str], ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TigerSchemaError("TIGER response is not UTF-8 HTML") from exc
    parser = _TigerTableParser()
    parser.feed(text)
    if not parser.rows or not parser.totals:
        raise TigerSchemaError("TIGER response contains no portfolio rows/count")
    totals = set(parser.totals)
    if len(totals) != 1 or next(iter(totals)) != len(parser.rows):
        raise TigerSchemaError("TIGER response row count is incomplete")
    parsed: list[tuple[str, str, str, str, str]] = []
    for row in parser.rows:
        if len(row) != 6:
            raise TigerSchemaError("TIGER portfolio row must contain exactly six columns")
        code, name, quantity, value, weight = row[:5]
        if not code or not name:
            raise TigerSchemaError("TIGER portfolio identity fields cannot be blank")
        parsed.append((code, name, quantity, value, weight))
    return tuple(parsed)


class TigerHoldingsAdapter:
    def __init__(
        self, client: TrustedHttpClient, workspace: SnapshotWorkspace,
        *, cutoff_date: date = DATA_CUTOFF_DATE,
    ) -> None:
        if cutoff_date != DATA_CUTOFF_DATE:
            raise ValueError("TIGER evaluation adapter cutoff must be 2026-08-24")
        self._client = client
        self._workspace = workspace
        self._cutoff = cutoff_date

    @staticmethod
    def source_url(product_isin: str, requested_date: date) -> str:
        if not _ISIN.fullmatch(product_isin):
            raise ValueError("TIGER product identifier must be a Korean ISIN")
        query = urlencode({
            "ksdFund": product_isin,
            "fixDate": requested_date.strftime("%Y.%m.%d"),
            "prfPrd": "Week01",
            "order": "SRD",
            "pageIndex": "1",
            "firstIndex": "0",
            "listCnt": "1000",
        })
        return f"{TIGER_ENDPOINT}?{query}"

    async def acquire(
        self, product: TigerProduct, *, requested_date: date = DATA_CUTOFF_DATE,
    ) -> TigerAdapterResult:
        product.validate()
        if requested_date > self._cutoff:
            raise ValueError("TIGER requested date cannot be after the evaluation cutoff")
        url = self.source_url(product.source_id, requested_date)
        self._workspace.add_source(TIGER_PROVIDER, url)
        fetch = await self._client.fetch(url)
        if fetch.content is None or fetch.content_hash is None:
            return TigerAdapterResult(fetch, None, (), None)
        artifact = self._workspace.preserve_raw(
            category="holdings", content=fetch.content, suffix="html",
            normalized_url=fetch.normalized_url, content_type=ContentType.HTML.value,
        )
        source_id = deterministic_source_record_id(
            source_provider=TIGER_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            normalized_url=fetch.normalized_url,
            raw_content_hash=fetch.content_hash,
        )
        try:
            parsed = parse_tiger_holdings_html(fetch.content)
            holdings = tuple(
                self._normalize(parsed, product, requested_date, source_id, fetch)
            )
        except (TigerSchemaError, ValueError) as exc:
            record = self._source_record(
                fetch, source_id, artifact.relative_path, requested_date,
                QualityStatus.PARSE_FAILED, product,
            )
            self._workspace.write_source_records(category="holdings", records=[record])
            raise TigerSchemaError("TIGER response failed the reviewed schema contract") from exc
        existing = self._workspace.get_source_record(source_id)
        record = existing or self._source_record(
            fetch, source_id, artifact.relative_path, requested_date,
            QualityStatus.VALID, product,
        )
        self._workspace.write_source_records(category="holdings", records=[record])
        # Bind normalized rows to the persisted evidence record.
        holdings = tuple(row.model_copy(update={
            "retrieved_at": record.retrieved_at,
            "source_record_id": record.source_record_id,
            "source_url": record.source_url,
            "source_trust_tier": record.source_trust_tier,
        }) for row in holdings)
        output = write_holdings(
            self._workspace, holdings, source_record_id=record.source_record_id,
        )
        return TigerAdapterResult(fetch, record, holdings, output, len(parsed))

    def _normalize(self, rows, product, effective_date, source_id, fetch):
        for code, name, quantity_raw, value_raw, weight_raw in rows:
            non_security = code.startswith("KRD") or "예금" in name or "현금" in name
            ticker = code if not non_security and _KRX_TICKER.fullmatch(code) else None
            if non_security:
                quantity = value = weight = None
                numeric_status = NumericStatus.RAW_ONLY
                identity_status = IdentityStatus.NON_SECURITY
            else:
                quantity = parse_nonnegative_decimal(quantity_raw, field="quantity")
                value = parse_nonnegative_decimal(value_raw, field="evaluated value")
                weight = _percent_to_proportion(weight_raw)
                numeric_status = NumericStatus.VALIDATED
                identity_status = (
                    IdentityStatus.SOURCE_ID_ONLY if ticker else IdentityStatus.UNRESOLVED
                )
            key = f"ticker:KRX:{ticker}" if ticker else f"provider_security_id:{code}"
            yield ExternalHolding(
                holding_record_id=deterministic_holding_id(
                    source_provider=TIGER_PROVIDER,
                    product_source_id=product.source_id,
                    constituent_key=key,
                    effective_date=effective_date,
                ),
                product_category=ProductCategory.DOMESTIC_ETF,
                product_name_raw=product.name,
                product_ticker=product.ticker,
                product_isin=product.isin,
                product_source_id=product.source_id,
                constituent_name_raw=name,
                constituent_ticker=ticker,
                constituent_source_id=code,
                weight_raw=weight_raw,
                weight_normalized=weight,
                weight_unit=WeightUnit.PERCENT_OF_NET_ASSET_VALUE if weight is not None else None,
                weight_scale=WeightScale.PERCENT_POINTS if weight is not None else None,
                quantity_raw=quantity_raw,
                quantity_normalized=quantity,
                quantity_unit=QuantityUnit.UNITS_PER_CREATION_UNIT if quantity is not None else None,
                market_value_raw=value_raw,
                market_value_normalized=value,
                market_value_currency="KRW" if value is not None else None,
                effective_date=effective_date,
                retrieved_at=fetch.retrieved_at,
                source_record_id=source_id,
                source_provider=TIGER_PROVIDER,
                source_url=fetch.requested_url,
                source_trust_tier=SourceTrustTier.AUTHORITATIVE,
                snapshot_id=self._workspace.snapshot_id,
                identity_status=identity_status,
                product_identity_status=IdentityStatus.VERIFIED_IDENTIFIER,
                constituent_identity_status=identity_status,
                numeric_status=numeric_status,
                temporal_status=TemporalStatus.EFFECTIVE_DATE_VERIFIED,
                validation_status=(
                    HoldingValidationStatus.VALID
                    if identity_status in {IdentityStatus.SOURCE_ID_ONLY, IdentityStatus.NON_SECURITY}
                    else HoldingValidationStatus.PARTIAL
                ),
            )

    def _source_record(self, fetch, source_id, artifact_path, effective_date,
                       quality_status, product):
        return ExternalSourceRecord(
            source_record_id=source_id,
            source_provider=TIGER_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            retrieved_at=fetch.retrieved_at,
            effective_date=effective_date,
            source_title="TIGER ETF Portfolio Deposit File",
            content_type=ContentType.HTML,
            http_status=fetch.status_code,
            raw_content_hash=fetch.content_hash or "",
            parser_version=TIGER_PARSER_VERSION,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=quality_status,
            raw_artifact_path=artifact_path,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            metadata={
                "provider_product_id": product.source_id,
                "product_isin": product.isin,
                "product_ticker": product.ticker,
                "exchange": product.exchange,
                "requested_fix_date": effective_date.isoformat(),
                "effective_date_contract": "exact official fixDate request",
                "weight_contract": "percent of net asset value",
            },
        )


def _percent_to_proportion(value: str) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("TIGER weight must be decimal percent points") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise ValueError("TIGER Security weight is outside 0..100 percent points")
    return parsed / Decimal("100")
