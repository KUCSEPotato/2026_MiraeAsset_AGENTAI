from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.ishares_production import audit_pref02_foreign_etfs
from app.external_data.holdings.models import (
    IdentityStatus,
    PositionCategory,
    PositionSemanticStatus,
    ProductCategory,
)
from app.external_data.holdings.providers.ishares import (
    ISharesHoldingsAdapter,
    ISharesProduct,
    ISharesSchemaError,
    parse_ishares_holdings_csv,
)
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace


CSV = b'''iShares Semiconductor ETF
Fund Holdings as of,"Jul 31, 2026"
Inception Date,"Jul 10, 2001"
Shares Outstanding,"1.00"
Stock,"-"
Bond,"-"
Cash,"-"
Other,"-"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date
"NVDA","NVIDIA CORP","Information Technology","Equity","100.00","8.25","100.00","5.00","20.00","United States","NASDAQ","USD","1.00","USD","-"
"USD","USD CASH","Cash and/or Derivatives","Cash","-1.00","-0.01","-1.00","-1.00","100.00","United States","-","USD","1.00","USD","-"
"IXTU6","EMINI TECHNOLOGY SELECT SECTOR SEP","Cash and/or Derivatives","Futures","0.00","0.00","10.00","1.00","10.00","-","Chicago Mercantile Exchange","USD","1.00","USD","-"
'''


def _settings(root: Path) -> ExternalCrawlerSettings:
    return ExternalCrawlerSettings(
        output_directory=root, request_interval_seconds=0,
        max_retries=0, respect_robots_txt=True,
    )


async def _acquire(root: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            200, content=CSV, headers={"content-type": "text/csv"}, request=request
        )

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    workspace = SnapshotWorkspace(
        root, snapshot_id="ishares-test", snapshot_date=date(2026, 9, 1),
        crawler_version="test", data_cutoff_date=DATA_CUTOFF_DATE,
    )
    client = TrustedHttpClient(_settings(root), client=raw)
    product = ISharesProduct(
        portfolio_id="239705", name="iShares Semiconductor ETF",
        ticker="SOXX", isin="US4642875235", exchange="XNAS",
    )
    adapter = ISharesHoldingsAdapter(client, workspace)
    first = await adapter.acquire(product, requested_date=date(2026, 7, 31))
    second = await adapter.acquire(product, requested_date=date(2026, 7, 31))
    await raw.aclose()
    return workspace, first, second


def test_official_historical_csv_contract_and_global_identity(tmp_path: Path) -> None:
    workspace, first, second = asyncio.run(_acquire(tmp_path))
    assert first.source_record is not None
    assert first.effective_date == date(2026, 7, 31)
    assert first.source_record.effective_date == date(2026, 7, 31)
    assert "asOfDate=20260731" in first.source_record.source_url
    equity = first.holdings[0]
    assert equity.product_category is ProductCategory.FOREIGN_ETF
    assert equity.constituent_ticker == "NVDA"
    assert equity.constituent_exchange == "XNAS"
    assert equity.constituent_identity_status is IdentityStatus.VERIFIED_IDENTIFIER
    assert equity.position_category is PositionCategory.EQUITY_SECURITY
    assert equity.position_semantic_status is PositionSemanticStatus.CANONICALIZABLE
    assert equity.weight_normalized == Decimal("0.0825")
    assert all(
        row.constituent_identity_status is IdentityStatus.NON_SECURITY
        for row in first.holdings[1:]
    )
    assert [row.holding_record_id for row in first.holdings] == [
        row.holding_record_id for row in second.holdings
    ]
    assert workspace.manifest.raw_file_count == 1


def test_csv_date_and_schema_fail_closed() -> None:
    with pytest.raises(ISharesSchemaError, match="portfolio date"):
        parse_ishares_holdings_csv(CSV.replace(b"Fund Holdings as of", b"Retrieved at"))
    with pytest.raises(ISharesSchemaError, match="schema"):
        parse_ishares_holdings_csv(CSV.replace(b"Market Currency", b"Unknown Currency"))


def test_pref02_foreign_etf_identity_audit() -> None:
    path = next(Path("material").glob("**/pref02n001_data.xlsx"))
    audit = audit_pref02_foreign_etfs(path)
    assert audit.foreign_etf_products == 5_972
    assert audit.with_isin == 5_960
    assert audit.with_ticker == 5_972
    assert audit.with_exchange == 5_972
    assert audit.unique_isin == 5_897
    assert audit.unique_ticker_exchange == 5_972
    assert {item.ticker for item in audit.reviewed_products} == {
        "EWY", "IVV", "IYW", "MCHI", "SOXX",
    }
