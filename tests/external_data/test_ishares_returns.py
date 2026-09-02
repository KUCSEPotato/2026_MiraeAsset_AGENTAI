from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.providers.ishares import ISharesProduct
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace
from app.external_data.metrics.ishares_returns import (
    ISharesPublishedReturnAdapter,
    ISharesReturnSchemaError,
    parse_ishares_one_year_return,
)


PRODUCT = ISharesProduct(
    portfolio_id="239705",
    name="iShares Semiconductor ETF",
    ticker="SOXX",
    isin="US4642875235",
    exchange="XNAS",
)
METHOD = (
    "Total return represents changes to the NAV and accounts for distributions "
    "from the fund."
)


def _response(
    *, product_id: str = "239705", as_of: str = "20260731",
    method: str = METHOD, value: object = 111.12681,
) -> bytes:
    return json.dumps({
        "productId": product_id,
        "currencyCode": "USD",
        "componentsByNameMap": {
            "performance": {
                "containersByNameMap": {
                    "returns": {
                        "subContainersByNameMap": {
                            "average": {
                                "dataPointsByNameMap": {
                                    "asOfDate": {"value": as_of},
                                    "returnTypes": {
                                        "value": ["navSourced", "marketPrice"],
                                        "infoBubble": [method, "Market price return"],
                                    },
                                    "oneYearAnnualized": {"value": [value, 110.5]},
                                }
                            }
                        }
                    }
                }
            }
        },
    }).encode()


def test_published_return_contract_is_exact_and_cutoff_safe() -> None:
    observation = parse_ishares_one_year_return(
        _response(), product=PRODUCT, requested_date=date(2026, 7, 31),
        source_record_id="source-1", source_url="https://official.example/performance",
    )
    assert observation.numeric_value == Decimal("111.12681")
    assert observation.observation_end_date == date(2026, 7, 31)
    assert observation.observation_start_date is None
    assert observation.return_basis == "NAV_TOTAL_RETURN"
    assert observation.distribution_treatment == "INCLUDED"
    assert observation.scale_basis == "ISHARES_NAV_TOTAL_RETURN_PCT_V1"
    assert observation.currency == "USD"
    assert observation.cutoff_valid


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (_response(product_id="other"), "product ID"),
        (_response(as_of="20260825"), "observation date"),
        (_response(method="changed"), "semantics changed"),
        (_response(value="NaN"), "finite"),
    ],
)
def test_published_return_schema_changes_fail_closed(
    content: bytes, message: str,
) -> None:
    with pytest.raises(ISharesReturnSchemaError, match=message):
        parse_ishares_one_year_return(
            content, product=PRODUCT, requested_date=date(2026, 7, 31),
            source_record_id="source-1", source_url="https://official.example/performance",
        )


def test_adapter_preserves_raw_and_semantic_identity_is_idempotent(
    tmp_path: Path,
) -> None:
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    200, text="User-agent: *\nAllow: /", request=request
                )
            return httpx.Response(
                200, content=_response(),
                headers={"content-type": "application/json"}, request=request,
            )

        raw = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        settings = ExternalCrawlerSettings(
            output_directory=tmp_path, request_interval_seconds=0,
            max_retries=0, respect_robots_txt=True,
        )
        workspace = SnapshotWorkspace(
            tmp_path, snapshot_id="ishares-return-test",
            snapshot_date=date(2026, 9, 2), crawler_version="test",
            data_cutoff_date=DATA_CUTOFF_DATE,
        )
        client = TrustedHttpClient(settings, client=raw)
        adapter = ISharesPublishedReturnAdapter(client, workspace)
        first = await adapter.acquire(PRODUCT, requested_date=date(2026, 7, 31))
        second = await adapter.acquire(PRODUCT, requested_date=date(2026, 7, 31))
        await raw.aclose()
        return workspace, first, second

    workspace, first, second = asyncio.run(run())
    assert first.status == second.status == "SUCCESS"
    assert first.observation is not None and second.observation is not None
    assert first.observation.metric_observation_id == (
        second.observation.metric_observation_id
    )
    assert first.observation.semantic_json() == second.observation.semantic_json()
    assert first.source_record is not None
    assert first.source_record.effective_date == date(2026, 7, 31)
    assert workspace.manifest.raw_file_count == 1
    assert workspace.manifest.source_record_count == 1
