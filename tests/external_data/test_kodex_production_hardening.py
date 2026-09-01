from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.external_data.config import ExternalCrawlerSettings
from app.external_data.holdings.catalog import (
    CatalogResolutionStatus,
    resolve_catalog_against_pref01,
)
from app.external_data.holdings.providers.kodex import KodexHoldingsAdapter, KodexProduct
from app.external_data.holdings.providers.kodex_catalog import (
    KodexCatalogAdapter,
    KodexCatalogProduct,
)
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import (
    ExternalSnapshotManifest,
    SnapshotStatus,
    SnapshotWorkspace,
    load_snapshot_manifest,
)
from app.external_data.models import ContentType, SourceQualityReport, SourceTrustTier


FIXTURE = (Path(__file__).parent / "fixtures/kodex_holdings_20260824.json").read_bytes()


def _settings(tmp_path: Path) -> ExternalCrawlerSettings:
    return ExternalCrawlerSettings(
        output_directory=tmp_path,
        request_interval_seconds=0.0,
        max_retries=0,
        respect_robots_txt=True,
    )


def _workspace(tmp_path: Path) -> SnapshotWorkspace:
    return SnapshotWorkspace(
        tmp_path,
        snapshot_id="hardening",
        snapshot_date=date(2026, 8, 31),
        crawler_version="test-v2",
        data_cutoff_date=date(2026, 8, 24),
    )


def _variant(*, curp: str | None = None, risep: str | None = None,
             rcv_time: str | None = None) -> bytes:
    value = json.loads(FIXTURE)
    if curp is not None:
        value["pdf"]["list"][0]["curp"] = curp
    if risep is not None:
        value["pdf"]["list"][0]["risep"] = risep
    if rcv_time is not None:
        value["pdf"]["rcvTime"] = rcv_time
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _run_variants(tmp_path: Path, bodies: list[bytes]):
    async def scenario():
        workspace = _workspace(tmp_path)
        pending = iter(bodies)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
            return httpx.Response(
                200,
                content=next(pending),
                headers={"Content-Type": "application/json"},
                request=request,
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TrustedHttpClient(_settings(tmp_path), client=async_client)
        adapter = KodexHoldingsAdapter(client, workspace)
        product = KodexProduct(
            source_id="2ETF15", name="KODEX 증권",
            ticker="102970", isin="KR7102970001",
        )
        results = []
        checksums = []
        for _ in bodies:
            results.append(await adapter.acquire(product))
            checksums.append(next(
                item.sha256 for item in workspace.manifest.normalized_outputs
                if item.relative_path == "holdings/normalized/holdings.jsonl"
            ))
        await async_client.aclose()
        return workspace, results, checksums

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "changed",
    [
        _variant(curp="999999"),
        _variant(risep="-12345"),
        _variant(rcv_time="235959"),
    ],
)
def test_volatile_market_fields_do_not_change_holding_ids(
    tmp_path: Path, changed: bytes,
) -> None:
    workspace, results, checksums = _run_variants(tmp_path, [FIXTURE, changed])
    first_ids = {row.holding_record_id for row in results[0].holdings}
    second_ids = {row.holding_record_id for row in results[1].holdings}
    assert first_ids == second_ids
    assert checksums[0] == checksums[1]
    assert workspace.manifest.raw_file_count == 2
    assert workspace.manifest.source_record_count == 2


def test_raw_hashes_stay_exact_and_distinct(tmp_path: Path) -> None:
    changed = _variant(curp="999999", rcv_time="235959")
    workspace, _, _ = _run_variants(tmp_path, [FIXTURE, changed])
    assert {item.sha256 for item in workspace.manifest.raw_artifacts} == {
        hashlib.sha256(FIXTURE).hexdigest(),
        hashlib.sha256(changed).hexdigest(),
    }


def test_multiple_source_records_support_one_semantic_holding(tmp_path: Path) -> None:
    workspace, results, _ = _run_variants(
        tmp_path, [FIXTURE, _variant(risep="777", rcv_time="222222")]
    )
    holding_ids = {row.holding_record_id for row in results[0].holdings}
    links = [
        json.loads(line)
        for line in (
            workspace.path / "holdings/normalized/holding_evidence_links.jsonl"
        ).read_text().splitlines()
    ]
    assert len(holding_ids) == 3
    assert len(links) == 6
    assert all(
        sum(link["holding_record_id"] == item for link in links) == 2
        for item in holding_ids
    )
    assert len({link["source_record_id"] for link in links}) == 2


def test_rerun_count_ids_and_semantic_checksum_are_stable(tmp_path: Path) -> None:
    workspace, results, checksums = _run_variants(
        tmp_path,
        [FIXTURE, _variant(curp="1", risep="2", rcv_time="3")],
    )
    path = workspace.path / "holdings/normalized/holdings.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == len(results[0].holdings) == len(results[1].holdings) == 3
    assert checksums[0] == checksums[1]
    assert all("retrieved_at" not in row and "source_record_id" not in row for row in rows)


def test_manifest_quality_report_round_trip_and_derived_failure_rate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    report = SourceQualityReport(
        provider="KODEX",
        trust_tier=SourceTrustTier.AUTHORITATIVE,
        access_method="HTTP",
        data_types=[ContentType.JSON],
        refresh_behavior="fixture",
        attempted_sources=4,
        successful_sources=3,
        failed_sources=1,
    )
    workspace.finalize(
        SnapshotStatus.PARTIAL,
        validation={"fixture": True},
        quality_reports=[report],
    )
    raw = json.loads((workspace.path / "manifest.json").read_text())
    assert "failure_rate" not in raw["source_quality_reports"][0]
    loaded = load_snapshot_manifest(
        tmp_path, snapshot_date=date(2026, 8, 31), snapshot_id="hardening"
    )
    assert loaded is not None
    assert loaded.source_quality_reports[0].failure_rate == 0.25


def test_manifest_strict_extra_field_validation_remains_enabled(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    value = workspace.manifest.model_dump(mode="json")
    value["unexpected"] = True
    with pytest.raises(ValidationError):
        ExternalSnapshotManifest.model_validate(value)


def _catalog_payload(*, ticker: str = "102970", current_price: str = "100") -> bytes:
    return json.dumps([{
        "fId": "2ETF15",
        "fNm": "KODEX 증권",
        "stkTicker": ticker,
        "totalCnt": "1",
        "curp": current_price,
        "risep": "1",
        "gijunYMD": "20260831",
    }], ensure_ascii=False).encode()


def test_catalog_parser_extracts_stable_identity() -> None:
    products, total = KodexCatalogAdapter._parse_page(_catalog_payload())
    assert total == 1
    assert products == [KodexCatalogProduct(
        source_id="2ETF15",
        name="KODEX 증권",
        ticker="102970",
        market="KRX",
        product_url="https://www.samsungfund.com/etf/product/view.do?id=2ETF15",
    )]


def test_catalog_identity_ignores_volatile_market_fields() -> None:
    first, _ = KodexCatalogAdapter._parse_page(_catalog_payload(current_price="100"))
    second, _ = KodexCatalogAdapter._parse_page(_catalog_payload(current_price="999"))
    assert first == second


def test_missing_volatile_rcv_time_is_accepted() -> None:
    payload = json.loads(FIXTURE)
    payload["pdf"].pop("rcvTime")
    from app.external_data.holdings.providers.kodex import KodexResponse

    response = KodexResponse.model_validate(payload)
    assert response.pdf.rcvTime is None
    assert response.pdf.effective_date == date(2026, 8, 24)


def _pref01(path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "pd_itm_no", "pd_abrv_nm", "pd_ticker", "pd_isin_cd",
        "pd_exg_mkt_cd", "pd_grp_no",
    ])
    for source_id, name, ticker, isin in rows:
        sheet.append([source_id, name, ticker, isin, "EXG_MKT_NO_001", "ETF"])
    workbook.save(path)
    return path


def test_catalog_pref01_ticker_resolution_is_exact(tmp_path: Path) -> None:
    path = _pref01(tmp_path / "pref01.xlsx", [
        ("KR7102970001", "KODEX 증권", "102970", "KR7102970001"),
    ])
    product = KodexCatalogProduct(
        source_id="2ETF15", name="KODEX 증권", ticker="102970",
        product_url="https://www.samsungfund.com/etf/product/view.do?id=2ETF15",
    )
    report = resolve_catalog_against_pref01((product,), path)
    assert report.matched_by_ticker == 1
    assert report.matched_products[0].isin == "KR7102970001"


def test_ambiguous_catalog_identity_fails_closed(tmp_path: Path) -> None:
    path = _pref01(tmp_path / "pref01.xlsx", [
        ("KR7102970001", "KODEX 증권 A", "102970", "KR7102970001"),
        ("KR7999990001", "KODEX 증권 B", "102970", "KR7999990001"),
    ])
    product = KodexCatalogProduct(
        source_id="2ETF15", name="KODEX 증권", ticker="102970",
        product_url="https://www.samsungfund.com/etf/product/view.do?id=2ETF15",
    )
    report = resolve_catalog_against_pref01((product,), path)
    assert report.ambiguous == 1
    assert not report.matched_products
    assert report.entries[0].status is CatalogResolutionStatus.AMBIGUOUS


def test_resume_partial_snapshot_preserves_existing_records(tmp_path: Path) -> None:
    workspace, _, checksums = _run_variants(tmp_path, [FIXTURE])
    workspace.finalize(SnapshotStatus.PARTIAL, validation={"retryable": True})
    resumed = SnapshotWorkspace.resume(
        tmp_path, snapshot_id="hardening", snapshot_date=date(2026, 8, 31)
    )
    assert resumed.source_record_ids == workspace.source_record_ids
    assert next(
        item.sha256 for item in resumed.manifest.normalized_outputs
        if item.relative_path == "holdings/normalized/holdings.jsonl"
    ) == checksums[0]


def test_hardening_path_does_not_create_canonical_v2_artifacts(tmp_path: Path) -> None:
    workspace, _, _ = _run_variants(tmp_path, [FIXTURE])
    assert not list(tmp_path.rglob("canonical_v2"))
    assert workspace.manifest.data_cutoff_date == date(2026, 8, 24)
