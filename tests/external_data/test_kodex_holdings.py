from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.holdings.contract import DATA_CUTOFF_DATE, PostCutoffHoldingError
from app.external_data.holdings.models import IdentityStatus, WeightUnit
from app.external_data.holdings.providers.kodex import (
    KodexHoldingsAdapter,
    KodexProduct,
    KodexSchemaError,
)
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotStatus, SnapshotWorkspace


FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = (FIXTURES / "kodex_holdings_20260824.json").read_bytes()


def _settings(tmp_path: Path) -> ExternalCrawlerSettings:
    return ExternalCrawlerSettings(
        output_directory=tmp_path,
        request_interval_seconds=0.0,
        max_retries=0,
        respect_robots_txt=True,
    )


def _workspace(tmp_path: Path, snapshot_id: str = "kodex-snapshot") -> SnapshotWorkspace:
    return SnapshotWorkspace(
        tmp_path,
        snapshot_id=snapshot_id,
        snapshot_date=date(2026, 8, 30),
        crawler_version="crawler-test-v1",
        data_cutoff_date=DATA_CUTOFF_DATE,
    )


def _handler(body: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(
            200, content=body, headers={"Content-Type": "application/json"}, request=request,
        )
    return handler


async def _acquire(
    tmp_path: Path, body: bytes = FIXTURE, *, workspace: SnapshotWorkspace | None = None,
):
    workspace = workspace or _workspace(tmp_path)
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(body)))
    client = TrustedHttpClient(_settings(tmp_path), client=async_client)
    adapter = KodexHoldingsAdapter(client, workspace)
    product = KodexProduct(
        source_id="2ETF15", name="KODEX 증권", ticker="102970", isin="KR7102970001",
    )
    try:
        result = await adapter.acquire(product)
    finally:
        await async_client.aclose()
    return workspace, result


def test_real_kodex_shape_normalizes_multiple_constituents_and_scale(tmp_path: Path) -> None:
    workspace, result = asyncio.run(_acquire(tmp_path))
    assert result.source_record is not None
    assert result.source_record.effective_date == date(2026, 8, 24)
    assert len(result.holdings) == 3

    equity = next(row for row in result.holdings if row.constituent_ticker == "006800")
    assert equity.constituent_source_id == "006800"
    assert equity.constituent_isin is None
    assert equity.weight_raw == "24.49"
    assert equity.weight_normalized == Decimal("0.2449")
    assert equity.weight_unit is WeightUnit.PERCENT_OF_NON_CASH_ASSETS
    assert equity.quantity_normalized == Decimal("6467")
    assert equity.market_value_normalized == Decimal("223111500")
    assert equity.market_value_currency == "KRW"
    assert equity.rank is None

    missing = next(row for row in result.holdings if row.constituent_source_id is None)
    assert missing.constituent_identity_status is IdentityStatus.NAME_ONLY
    assert missing.validation_status.value == "PARTIAL"

    raw = workspace.path / result.source_record.raw_artifact_path
    normalized = workspace.path / result.normalized_output.relative_path  # type: ignore[union-attr]
    assert raw.read_bytes() == FIXTURE
    rows = [json.loads(line) for line in normalized.read_text().splitlines()]
    assert all("source_record_id" not in row and "retrieved_at" not in row for row in rows)
    evidence = [
        json.loads(line)
        for line in (
            workspace.path / "holdings/normalized/holding_evidence_links.jsonl"
        ).read_text().splitlines()
    ]
    assert {row["source_record_id"] for row in evidence} == {
        result.source_record.source_record_id
    }
    assert {row["holding_record_id"] for row in evidence} == {
        row.holding_record_id for row in result.holdings
    }
    assert workspace.manifest.data_cutoff_date == DATA_CUTOFF_DATE


def test_holding_ids_and_rerun_are_idempotent(tmp_path: Path) -> None:
    async def scenario():
        workspace = _workspace(tmp_path)
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(FIXTURE)))
        client = TrustedHttpClient(_settings(tmp_path), client=async_client)
        adapter = KodexHoldingsAdapter(client, workspace)
        product = KodexProduct(
            source_id="2ETF15", name="KODEX 증권", ticker="102970", isin="KR7102970001",
        )
        first = await adapter.acquire(product)
        first_payload = (workspace.path / first.normalized_output.relative_path).read_bytes()  # type: ignore[union-attr]
        second = await adapter.acquire(product)
        second_payload = (workspace.path / second.normalized_output.relative_path).read_bytes()  # type: ignore[union-attr]
        await async_client.aclose()
        return workspace, first, second, first_payload, second_payload

    workspace, first, second, first_payload, second_payload = asyncio.run(scenario())
    assert [row.holding_record_id for row in first.holdings] == [
        row.holding_record_id for row in second.holdings
    ]
    assert first_payload == second_payload
    assert workspace.manifest.raw_file_count == 1
    assert workspace.manifest.source_record_count == 1
    assert second.normalized_output is not None and second.normalized_output.row_count == 3


def test_cutoff_accepts_20260824_and_rejects_post_cutoff_response(tmp_path: Path) -> None:
    _, accepted = asyncio.run(_acquire(tmp_path / "accepted"))
    assert all(row.effective_date == DATA_CUTOFF_DATE for row in accepted.holdings)

    payload = json.loads(FIXTURE)
    payload["pdf"]["gijunYMD"] = "20260825"
    with pytest.raises(PostCutoffHoldingError):
        asyncio.run(_acquire(tmp_path / "rejected", json.dumps(payload).encode()))
    workspace_path = tmp_path / "rejected" / "snapshots" / "2026-08-30" / "kodex-snapshot"
    assert list((workspace_path / "holdings" / "raw").iterdir())
    assert not (workspace_path / "holdings" / "normalized" / "holdings.jsonl").exists()
    manifest = json.loads((workspace_path / "manifest.json").read_text())
    assert manifest["failures"][0]["quality_status"] == "VALIDATION_FAILED"


def test_malformed_schema_fails_visibly_after_preserving_raw(tmp_path: Path) -> None:
    payload = json.loads(FIXTURE)
    payload["pdf"]["list"][0]["unexpected"] = "schema drift"
    body = json.dumps(payload).encode()
    with pytest.raises(KodexSchemaError):
        asyncio.run(_acquire(tmp_path, body))
    workspace_path = tmp_path / "snapshots" / "2026-08-30" / "kodex-snapshot"
    assert next((workspace_path / "holdings" / "raw").iterdir()).read_bytes() == body
    manifest = json.loads((workspace_path / "manifest.json").read_text())
    assert manifest["failures"][0]["quality_status"] == "PARSE_FAILED"
    assert not (workspace_path / "holdings" / "normalized" / "holdings.jsonl").exists()


def test_capability_probe_can_group_products_by_source_constituent(tmp_path: Path) -> None:
    _, result = asyncio.run(_acquire(tmp_path))
    reverse: dict[str, set[str]] = {}
    for row in result.holdings:
        if row.constituent_source_id:
            reverse.setdefault(row.constituent_source_id, set()).add(row.product_source_id)
    assert reverse["006800"] == {"2ETF15"}
    assert all(not hasattr(row, "canonical_product_id") for row in result.holdings)


def test_manifest_registers_source_and_holdings_outputs(tmp_path: Path) -> None:
    workspace, result = asyncio.run(_acquire(tmp_path))
    manifest = workspace.finalize(
        SnapshotStatus.READY,
        validation={"kodex_contract": True, "cutoff_enforced": True, "canonical_v2_writes": 0},
    )
    assert result.source_record is not None
    assert manifest.normalized_row_counts == {
        "external-source-record-v1": 1,
        "external-holdings-v1": 3,
        "external-holding-evidence-link-v1": 3,
    }
    assert manifest.raw_file_count == 1
