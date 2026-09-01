"""Official BlackRock/iShares historical holdings CSV adapter.

Only the date-qualified ``get-fund-document`` endpoint is accepted.  The CSV
itself must state its portfolio date; the request date and ``retrieved_at`` are
never used as a substitute for that semantic date.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from io import StringIO
from urllib.parse import urlencode

from app.external_data.holdings.contract import (
    DATA_CUTOFF_DATE,
    HoldingsContractError,
    write_holdings,
)
from app.external_data.holdings.models import (
    ExternalHolding,
    HoldingValidationStatus,
    IdentityStatus,
    NumericStatus,
    PositionCategory,
    PositionSemanticStatus,
    ProductCategory,
    QuantityUnit,
    TemporalStatus,
    WeightScale,
    WeightUnit,
    deterministic_holding_id,
)
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


ISHARES_PROVIDER = "BlackRock iShares"
ISHARES_PARSER_VERSION = "ishares-us-holdings-csv-v1"
ISHARES_DOCUMENT_ENDPOINT = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v1/get-fund-document"
)
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_TICKER = re.compile(r"[A-Z0-9.\-]{1,16}\Z")
_EXPECTED_COLUMNS = (
    "Ticker", "Name", "Sector", "Asset Class", "Market Value", "Weight (%)",
    "Notional Value", "Quantity", "Price", "Location", "Exchange", "Currency",
    "FX Rate", "Market Currency", "Accrual Date",
)

# Exact official exchange labels observed in the reviewed historical source.
# Values are ISO 10383 MICs except KRX, which intentionally preserves the
# existing domestic canonical namespace used by KODEX/TIGER.
ISHARES_EXCHANGE_NAMESPACES = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "Cboe BZX": "BATS",
    "Shanghai Stock Exchange": "XSHG",
    "Shenzhen Stock Exchange": "XSHE",
    "Hong Kong Exchanges And Clearing Ltd": "XHKG",
    "Korea Exchange (Stock Market)": "KRX",
    "Korea Exchange (Kosdaq)": "KRX",
}


class ISharesSchemaError(HoldingsContractError):
    pass


@dataclass(frozen=True, slots=True)
class ISharesProduct:
    portfolio_id: str
    name: str
    ticker: str
    isin: str
    exchange: str

    @property
    def source_id(self) -> str:
        return self.isin

    def validate(self) -> None:
        if not self.portfolio_id.isdigit() or not self.name.strip():
            raise ValueError("iShares product requires an official numeric portfolio ID and name")
        if not _TICKER.fullmatch(self.ticker) or not _ISIN.fullmatch(self.isin):
            raise ValueError("iShares product requires validated PREF02 ticker and ISIN")
        if not re.fullmatch(r"[A-Z]{4}", self.exchange):
            raise ValueError("iShares product exchange must be a four-character MIC")


@dataclass(frozen=True, slots=True)
class ISharesAdapterResult:
    fetch: FetchResult
    source_record: ExternalSourceRecord | None
    holdings: tuple[ExternalHolding, ...]
    normalized_output: NormalizedOutputEntry | None
    effective_date: date | None = None


def parse_ishares_holdings_csv(content: bytes) -> tuple[str, date, tuple[dict[str, str], ...]]:
    try:
        lines = content.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ISharesSchemaError("iShares holdings response is not UTF-8 CSV") from exc
    if len(lines) < 11:
        raise ISharesSchemaError("iShares holdings CSV is structurally incomplete")
    title = lines[0].strip()
    date_row = next(csv.reader([lines[1]]), [])
    if len(date_row) != 2 or date_row[0].strip() != "Fund Holdings as of":
        raise ISharesSchemaError("iShares CSV lacks an authoritative portfolio date")
    try:
        effective_date = datetime.strptime(date_row[1].strip(), "%b %d, %Y").date()
    except ValueError as exc:
        raise ISharesSchemaError("iShares portfolio date has an unknown format") from exc
    header_index = next(
        (index for index, value in enumerate(lines) if value.startswith("Ticker,Name,")),
        None,
    )
    if header_index is None:
        raise ISharesSchemaError("iShares CSV holdings header is missing")
    reader = csv.DictReader(StringIO("\n".join(lines[header_index:])))
    if tuple(reader.fieldnames or ()) != _EXPECTED_COLUMNS:
        raise ISharesSchemaError("iShares CSV schema no longer matches the reviewed contract")
    rows = tuple({key: str(value or "").strip() for key, value in row.items()} for row in reader)
    if not title or not rows:
        raise ISharesSchemaError("iShares CSV title/holdings rows cannot be empty")
    return title, effective_date, rows


class ISharesHoldingsAdapter:
    def __init__(
        self,
        client: TrustedHttpClient,
        workspace: SnapshotWorkspace,
        *,
        cutoff_date: date = DATA_CUTOFF_DATE,
    ) -> None:
        if cutoff_date != DATA_CUTOFF_DATE:
            raise ValueError("iShares evaluation adapter cutoff must be 2026-08-24")
        self._client = client
        self._workspace = workspace
        self._cutoff = cutoff_date

    @staticmethod
    def source_url(portfolio_id: str, requested_date: date) -> str:
        if not portfolio_id.isdigit():
            raise ValueError("iShares portfolio ID must be numeric")
        query = urlencode({
            "appType": "PRODUCT_PAGE",
            "appSubType": "ISHARES",
            "targetSite": "us-ishares",
            "locale": "en_US",
            "portfolioId": portfolio_id,
            "userType": "individual",
            "asOfDate": requested_date.strftime("%Y%m%d"),
            "component": "holdings",
        })
        return f"{ISHARES_DOCUMENT_ENDPOINT}?{query}"

    async def acquire(
        self,
        product: ISharesProduct,
        *,
        requested_date: date,
    ) -> ISharesAdapterResult:
        product.validate()
        if requested_date > self._cutoff:
            raise ValueError("iShares requested date cannot be after the evaluation cutoff")
        url = self.source_url(product.portfolio_id, requested_date)
        self._workspace.add_source(ISHARES_PROVIDER, url)
        fetch = await self._client.fetch(url)
        if fetch.content is None or fetch.content_hash is None:
            return ISharesAdapterResult(fetch, None, (), None)
        artifact = self._workspace.preserve_raw(
            category="holdings", content=fetch.content, suffix="csv",
            normalized_url=fetch.normalized_url, content_type=ContentType.CSV.value,
        )
        source_id = deterministic_source_record_id(
            source_provider=ISHARES_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            normalized_url=fetch.normalized_url,
            raw_content_hash=fetch.content_hash,
        )
        try:
            title, effective_date, parsed = parse_ishares_holdings_csv(fetch.content)
            if effective_date > requested_date or effective_date > self._cutoff:
                raise ISharesSchemaError("iShares response is newer than the requested/cutoff date")
            if _normalized_name(title) != _normalized_name(product.name):
                raise ISharesSchemaError("iShares response product title does not match PREF02")
            holdings = tuple(
                self._normalize(parsed, product, effective_date, source_id, fetch)
            )
        except (ISharesSchemaError, ValueError) as exc:
            record = self._source_record(
                fetch, source_id, artifact.relative_path, requested_date,
                QualityStatus.PARSE_FAILED, product,
            )
            self._workspace.write_source_records(category="holdings", records=[record])
            raise ISharesSchemaError("iShares response failed the reviewed contract") from exc
        existing = self._workspace.get_source_record(source_id)
        record = existing or self._source_record(
            fetch, source_id, artifact.relative_path, effective_date,
            QualityStatus.VALID, product,
        )
        self._workspace.write_source_records(category="holdings", records=[record])
        holdings = tuple(row.model_copy(update={
            "retrieved_at": record.retrieved_at,
            "source_record_id": record.source_record_id,
            "source_url": record.source_url,
            "source_trust_tier": record.source_trust_tier,
        }) for row in holdings)
        output = write_holdings(
            self._workspace, holdings, source_record_id=record.source_record_id,
        )
        return ISharesAdapterResult(fetch, record, holdings, output, effective_date)

    def _normalize(self, rows, product, effective_date, source_id, fetch):
        for rank, row in enumerate(rows, start=1):
            ticker = row["Ticker"].upper()
            asset_class = row["Asset Class"]
            exchange = ISHARES_EXCHANGE_NAMESPACES.get(row["Exchange"])
            category = _position_category(asset_class)
            canonicalizable = (
                category is PositionCategory.EQUITY_SECURITY
                and exchange is not None
                and _TICKER.fullmatch(ticker) is not None
                and ticker != "-"
            )
            non_security = category in {
                PositionCategory.CASH,
                PositionCategory.MONEY_MARKET,
                PositionCategory.DERIVATIVE,
                PositionCategory.FX,
            }
            identity_status = (
                IdentityStatus.VERIFIED_IDENTIFIER if canonicalizable
                else IdentityStatus.NON_SECURITY if non_security
                else IdentityStatus.UNRESOLVED
            )
            semantic_status = (
                PositionSemanticStatus.CANONICALIZABLE if canonicalizable
                else PositionSemanticStatus.NON_SECURITY if non_security
                else PositionSemanticStatus.UNSUPPORTED
            )
            weight = _percent(row["Weight (%)"]) if canonicalizable else None
            quantity = _nonnegative(row["Quantity"]) if canonicalizable else None
            market_value = _nonnegative(row["Market Value"]) if canonicalizable else None
            constituent_key = (
                f"ticker:{exchange}:{ticker}"
                if canonicalizable
                else f"position:{rank}:{asset_class}:{ticker}:{row['Name']}"
            )
            yield ExternalHolding(
                holding_record_id=deterministic_holding_id(
                    source_provider=ISHARES_PROVIDER,
                    product_source_id=product.source_id,
                    constituent_key=constituent_key,
                    effective_date=effective_date,
                ),
                product_category=ProductCategory.FOREIGN_ETF,
                product_name_raw=product.name,
                product_ticker=product.ticker,
                product_isin=product.isin,
                product_exchange=product.exchange,
                product_source_id=product.source_id,
                constituent_name_raw=row["Name"],
                constituent_ticker=ticker if canonicalizable else None,
                constituent_exchange=exchange if canonicalizable else None,
                constituent_source_id=(
                    f"{exchange}:{ticker}" if canonicalizable else None
                ),
                constituent_instrument_type=asset_class,
                position_category=category,
                position_semantic_status=semantic_status,
                weight_raw=row["Weight (%)"],
                weight_normalized=weight,
                weight_unit=(
                    WeightUnit.PERCENT_OF_NET_ASSET_VALUE if weight is not None else None
                ),
                weight_scale=WeightScale.PERCENT_POINTS if weight is not None else None,
                quantity_raw=row["Quantity"],
                quantity_normalized=quantity,
                quantity_unit=QuantityUnit.UNITS if quantity is not None else None,
                market_value_raw=row["Market Value"],
                market_value_normalized=market_value,
                market_value_currency=row["Currency"] if market_value is not None else None,
                rank=rank,
                effective_date=effective_date,
                retrieved_at=fetch.retrieved_at,
                source_record_id=source_id,
                source_provider=ISHARES_PROVIDER,
                source_url=fetch.requested_url,
                source_trust_tier=SourceTrustTier.AUTHORITATIVE,
                snapshot_id=self._workspace.snapshot_id,
                identity_status=identity_status,
                product_identity_status=IdentityStatus.VERIFIED_IDENTIFIER,
                constituent_identity_status=identity_status,
                numeric_status=(
                    NumericStatus.VALIDATED if canonicalizable else NumericStatus.RAW_ONLY
                ),
                temporal_status=TemporalStatus.EFFECTIVE_DATE_VERIFIED,
                validation_status=(
                    HoldingValidationStatus.VALID
                    if canonicalizable or non_security
                    else HoldingValidationStatus.PARTIAL
                ),
            )

    def _source_record(self, fetch, source_id, artifact_path, effective_date,
                       quality_status, product):
        return ExternalSourceRecord(
            source_record_id=source_id,
            source_provider=ISHARES_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            retrieved_at=fetch.retrieved_at,
            effective_date=effective_date,
            source_title="iShares Historical Fund Holdings",
            content_type=ContentType.CSV,
            http_status=fetch.status_code,
            raw_content_hash=fetch.content_hash or "",
            parser_version=ISHARES_PARSER_VERSION,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=quality_status,
            raw_artifact_path=artifact_path,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            metadata={
                "portfolio_id": product.portfolio_id,
                "product_isin": product.isin,
                "product_ticker": product.ticker,
                "product_exchange_mic": product.exchange,
                "requested_as_of_date": effective_date.isoformat(),
                "effective_date_contract": "CSV Fund Holdings as of field",
                "identity_contract": "exchange/MIC plus ticker; no name fallback",
            },
        )


def _position_category(asset_class: str) -> PositionCategory:
    return {
        "Equity": PositionCategory.EQUITY_SECURITY,
        "Cash": PositionCategory.CASH,
        "Cash Collateral and Margins": PositionCategory.CASH,
        "Money Market": PositionCategory.MONEY_MARKET,
        "Futures": PositionCategory.DERIVATIVE,
        "Options": PositionCategory.DERIVATIVE,
        "FX": PositionCategory.FX,
    }.get(asset_class, PositionCategory.OTHER)


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("iShares numeric field is not decimal") from exc
    if not parsed.is_finite():
        raise ValueError("iShares numeric field must be finite")
    return parsed


def _percent(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed < 0 or parsed > 100:
        raise ValueError("iShares Security weight is outside 0..100 percent points")
    return parsed / Decimal("100")


def _nonnegative(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed < 0:
        raise ValueError("iShares Security numeric value cannot be negative")
    return parsed
