from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.models import IdentityStatus, WeightUnit
from app.external_data.holdings.providers.tiger import (
    TigerHoldingsAdapter,
    TigerProduct,
    TigerSchemaError,
    parse_tiger_holdings_html,
)
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace


HTML = b"""
<tr data-tot-cnt="2"><td>005930</td><td>Samsung</td><td>6,984</td>
<td>1,815,840,000</td><td>33.62</td><td><span>down</span>-8.70</td></tr>
<tr data-tot-cnt="2"><td>KRD010010001</td><td>KRW cash</td><td>-1</td>
<td>-100</td><td>-0.01</td><td>0</td></tr>
"""


def _workspace(root: Path) -> SnapshotWorkspace:
    return SnapshotWorkspace(
        root, snapshot_id="tiger-test", snapshot_date=date(2026, 9, 1),
        crawler_version="test", data_cutoff_date=DATA_CUTOFF_DATE,
    )


def _settings(root: Path) -> ExternalCrawlerSettings:
    return ExternalCrawlerSettings(
        output_directory=root, request_interval_seconds=0,
        max_retries=0, respect_robots_txt=True,
    )


async def _acquire(root: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"}, request=request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    workspace = _workspace(root)
    client = TrustedHttpClient(_settings(root), client=raw)
    adapter = TigerHoldingsAdapter(client, workspace)
    product = TigerProduct(
        source_id="KR7102110004", name="TIGER 200",
        ticker="102110", isin="KR7102110004",
    )
    first = await adapter.acquire(product)
    second = await adapter.acquire(product)
    await raw.aclose()
    return workspace, first, second


def test_tiger_official_table_contract_and_exact_cutoff(tmp_path: Path) -> None:
    workspace, first, second = asyncio.run(_acquire(tmp_path))
    assert first.source_record is not None
    assert first.source_record.effective_date == DATA_CUTOFF_DATE
    assert first.source_record.metadata["requested_fix_date"] == "2026-08-24"
    assert "fixDate=2026.08.24" in first.source_record.source_url
    assert first.expected_row_count == 2
    equity = next(row for row in first.holdings if row.constituent_ticker == "005930")
    assert equity.weight_normalized == Decimal("0.3362")
    assert equity.weight_unit is WeightUnit.PERCENT_OF_NET_ASSET_VALUE
    cash = next(row for row in first.holdings if row.constituent_ticker is None)
    assert cash.constituent_identity_status is IdentityStatus.NON_SECURITY
    assert cash.quantity_raw == "-1" and cash.quantity_normalized is None
    assert [row.holding_record_id for row in first.holdings] == [
        row.holding_record_id for row in second.holdings
    ]
    assert workspace.manifest.raw_file_count == 1


def test_tiger_row_count_is_fail_closed() -> None:
    with pytest.raises(TigerSchemaError, match="incomplete"):
        parse_tiger_holdings_html(HTML.replace(b'data-tot-cnt="2"', b'data-tot-cnt="3"'))


def test_tiger_product_identity_is_exact() -> None:
    with pytest.raises(ValueError, match="ISIN"):
        TigerProduct(
            source_id="TIGER200", name="TIGER 200",
            ticker="102110", isin="KR7102110004",
        ).validate()
