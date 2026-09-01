from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

from app.external_data.issuers.krx_kind import (
    build_krx_kind_issuer_snapshot,
    load_issuer_records,
    parse_krx_kind_companies,
)
from app.external_data.manifest import SnapshotStatus, SnapshotWorkspace


def _html(rows: list[tuple[str, str, str, str]]) -> bytes:
    body = "".join(
        "<tr>"
        f"<td><a title='{name}' onclick=\"companysummary_open('{issuer}')\">{name}</a></td>"
        "<td>주권</td>"
        f"<td>{ticker}</td><td>{listed}</td>"
        "</tr>"
        for issuer, name, ticker, listed in rows
    )
    return f"<html><body><table>{body}</table></body></html>".encode("euc-kr")


def test_kind_parser_extracts_exchange_identity_without_name_inference() -> None:
    rows = parse_krx_kind_companies(
        _html([("00593", "삼성전자", "005930", "1975-06-11")]),
        market_id="STK",
        market_name="KOSPI",
    )
    assert len(rows) == 1
    assert rows[0].issuer_code == "00593"
    assert rows[0].representative_ticker == "005930"
    assert rows[0].issuer_name == "삼성전자"
    assert rows[0].listing_date == date(1975, 6, 11)


def test_kind_parser_rejects_duplicate_or_malformed_tickers() -> None:
    duplicated = _html([
        ("00593", "삼성전자", "005930", "1975-06-11"),
        ("00593", "삼성전자", "005930", "1975-06-11"),
    ])
    with pytest.raises(ValueError, match="duplicate"):
        parse_krx_kind_companies(
            duplicated, market_id="STK", market_name="KOSPI"
        )
    with pytest.raises(ValueError, match="ticker"):
        parse_krx_kind_companies(
            _html([("00593", "삼성전자", "5930", "1975-06-11")]),
            market_id="STK",
            market_name="KOSPI",
        )


def test_exact_cutoff_snapshot_preserves_raw_and_leaves_preferred_share_unresolved(
    tmp_path,
) -> None:
    market_rows = {
        "STK": _html([
            ("00593", "삼성전자", "005930", "1975-06-11"),
            ("00066", "SK하이닉스", "000660", "1996-12-26"),
        ]),
        "KSQ": _html([("03542", "NAVER", "035420", "2008-11-28")]),
        "KNX": _html([("95020", "테스트", "950200", "2020-01-01")]),
    }

    class Client:
        async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
            assert data["selDate"] == "20260824"
            assert data["secugrpId"] == "ST"
            request = httpx.Request("POST", url)
            return httpx.Response(
                200, content=market_rows[data["mktId"]], request=request
            )

    workspace = SnapshotWorkspace(
        tmp_path,
        snapshot_id="krx-issuer-test",
        snapshot_date=date(2026, 8, 31),
        crawler_version="test",
        data_cutoff_date=date(2026, 8, 24),
    )
    result = asyncio.run(build_krx_kind_issuer_snapshot(
        Client(),
        workspace,
        scoped_tickers={"005930", "000660", "005935"},
    ))

    assert workspace.manifest.status is SnapshotStatus.READY
    assert result.scoped_security_count == 3
    assert len(result.records) == 2
    assert result.unresolved_tickers == ("005935",)
    assert result.artifact_count == 3
    assert result.source_record_count == 3
    assert len(load_issuer_records(workspace.path)) == 2
    assert all(item.effective_date == date(2026, 8, 24) for item in result.records)
    source_payload = result.records[0].model_dump()
    assert not any(key.startswith("canonical_") for key in source_payload)

