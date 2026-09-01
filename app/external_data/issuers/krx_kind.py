"""KRX KIND exact-cutoff listed-company issuer adapter.

The official company-grain result exposes the representative security code,
company name, listing date, and KIND ``isurCd`` used by the exchange's own
company-summary link.  No display-name inference is used.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import httpx

from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.issuers.models import (
    EXTERNAL_SECURITY_ISSUER_SCHEMA,
    ExternalSecurityIssuerRecord,
    IssuerIdentityStatus,
    deterministic_issuer_record_id,
)
from app.external_data.manifest import SnapshotStatus, SnapshotWorkspace
from app.external_data.models import (
    ContentType,
    ExternalSourceRecord,
    QualityStatus,
    SourceQualityReport,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)


KRX_KIND_PROVIDER = "Korea Exchange KIND"
KRX_KIND_ENDPOINT = "https://kind.krx.co.kr/corpgeneral/listedissuestatusdetail.do"
KRX_KIND_PARSER_VERSION = "krx-kind-listed-company-v1"
KRX_KIND_ISSUER_SNAPSHOT_SCHEMA = "krx-kind-security-issuer-snapshot-v1"
_ISSUER_CODE = re.compile(r"[A-Z0-9]{5}\Z")
_SECURITY_CODE = re.compile(r"[A-Z0-9]{6}\Z")
_COMPANY_LINK = re.compile(r"companysummary_open\('([A-Z0-9]{5})'\)")
_MARKETS = (("STK", "KOSPI"), ("KSQ", "KOSDAQ"), ("KNX", "KONEX"))


class AsyncFormClient(Protocol):
    async def post(self, url: str, *, data: dict[str, str]) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class KrxKindCompanyRow:
    market_id: str
    market_name: str
    issuer_name: str
    issuer_code: str
    representative_ticker: str
    listing_date: date


@dataclass(frozen=True, slots=True)
class KrxIssuerSnapshotResult:
    scoped_security_count: int
    records: tuple[ExternalSecurityIssuerRecord, ...]
    unresolved_tickers: tuple[str, ...]
    conflicting_tickers: tuple[str, ...]
    source_record_count: int
    artifact_count: int


class _CompanyTableParser(HTMLParser):
    def __init__(self, market_id: str, market_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.market_id = market_id
        self.market_name = market_name
        self.rows: list[KrxKindCompanyRow] = []
        self._in_row = False
        self._cell: list[str] | None = None
        self._cells: list[str] = []
        self._issuer_code: str | None = None
        self._issuer_name: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._issuer_code = None
            self._issuer_name = None
        elif self._in_row and tag in {"td", "th"}:
            self._cell = []
        elif self._in_row and tag == "a":
            match = _COMPANY_LINK.search(values.get("onclick") or "")
            if match is not None:
                self._issuer_code = match.group(1)
                self._issuer_name = html.unescape(values.get("title") or "").strip()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._in_row:
            self._finish_row()
            self._in_row = False

    def _finish_row(self) -> None:
        if self._issuer_code is None or self._issuer_name is None:
            return
        if len(self._cells) < 4:
            raise ValueError("KRX KIND company row has an unexpected schema")
        ticker = self._cells[2].strip().upper()
        if not _SECURITY_CODE.fullmatch(ticker):
            raise ValueError("KRX KIND representative ticker is invalid")
        if not _ISSUER_CODE.fullmatch(self._issuer_code):
            raise ValueError("KRX KIND isurCd is invalid")
        listing_date = date.fromisoformat(self._cells[3])
        self.rows.append(KrxKindCompanyRow(
            market_id=self.market_id,
            market_name=self.market_name,
            issuer_name=self._issuer_name,
            issuer_code=self._issuer_code,
            representative_ticker=ticker,
            listing_date=listing_date,
        ))


def parse_krx_kind_companies(
    content: bytes, *, market_id: str, market_name: str,
) -> tuple[KrxKindCompanyRow, ...]:
    decoded: str | None = None
    for encoding in ("utf-8", "euc-kr"):
        try:
            decoded = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise ValueError("KRX KIND response is not supported Korean HTML")
    parser = _CompanyTableParser(market_id, market_name)
    parser.feed(decoded)
    if not parser.rows:
        raise ValueError("KRX KIND response contains no listed-company rows")
    keys = [(row.market_id, row.representative_ticker) for row in parser.rows]
    if len(keys) != len(set(keys)):
        raise ValueError("KRX KIND response contains duplicate company tickers")
    return tuple(parser.rows)


async def build_krx_kind_issuer_snapshot(
    client: AsyncFormClient,
    workspace: SnapshotWorkspace,
    *,
    scoped_tickers: set[str],
    cutoff: date = DATA_CUTOFF_DATE,
) -> KrxIssuerSnapshotResult:
    if cutoff != DATA_CUTOFF_DATE:
        raise ValueError("KRX issuer cutoff must be 2026-08-24")
    if not scoped_tickers or any(
        re.fullmatch(r"\d{6}", ticker) is None for ticker in scoped_tickers
    ):
        raise ValueError("issuer scope must contain exact six-digit KRX tickers")

    by_ticker: dict[str, list[tuple[KrxKindCompanyRow, ExternalSourceRecord]]] = {}
    sources: list[ExternalSourceRecord] = []
    for market_id, market_name in _MARKETS:
        form = {
            "method": "searchListedIssueStatDetailSub",
            "forward": "listedissuestatdetail_sub",
            "currentPageSize": "3000",
            "pageIndex": "1",
            "selDate": cutoff.strftime("%Y%m%d"),
            "mktId": market_id,
            "secugrpId": "ST",
            "detailType": "1",
        }
        response = await client.post(KRX_KIND_ENDPOINT, data=form)
        response.raise_for_status()
        if len(response.content) > 8 * 1024 * 1024:
            raise ValueError("KRX KIND response exceeds the bounded adapter limit")
        source_url = KRX_KIND_ENDPOINT + "?" + urlencode(form)
        artifact = workspace.preserve_raw(
            category="issuers", content=response.content, suffix="html",
            normalized_url=source_url, content_type="HTML",
        )
        source_id = deterministic_source_record_id(
            source_provider=KRX_KIND_PROVIDER,
            source_type=SourceType.EXCHANGE,
            normalized_url=source_url,
            raw_content_hash=artifact.sha256,
        )
        source = ExternalSourceRecord(
            source_record_id=source_id,
            source_provider=KRX_KIND_PROVIDER,
            source_type=SourceType.EXCHANGE,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=source_url,
            retrieved_at=datetime.now(UTC),
            effective_date=cutoff,
            source_title=f"KRX KIND listed companies {market_name} at {cutoff}",
            content_type=ContentType.HTML,
            http_status=response.status_code,
            raw_content_hash=artifact.sha256,
            parser_version=KRX_KIND_PARSER_VERSION,
            crawler_version=workspace.manifest.crawler_version,
            snapshot_id=workspace.snapshot_id,
            quality_status=QualityStatus.VALID,
            raw_artifact_path=artifact.relative_path,
            normalized_url=source_url,
            metadata={
                "market_id": market_id,
                "market_name": market_name,
                "cutoff_parameter": cutoff.isoformat(),
                "grain": "listed_company",
                "issuer_identifier_field": "isurCd",
            },
        )
        sources.append(source)
        workspace.add_source(KRX_KIND_PROVIDER, source_url)
        for row in parse_krx_kind_companies(
            response.content, market_id=market_id, market_name=market_name,
        ):
            if row.representative_ticker in scoped_tickers:
                by_ticker.setdefault(row.representative_ticker, []).append((row, source))

    conflicts = {
        ticker for ticker, candidates in by_ticker.items()
        if len({(row.issuer_code, row.issuer_name) for row, _ in candidates}) != 1
    }
    records: list[ExternalSecurityIssuerRecord] = []
    for ticker in sorted(scoped_tickers - conflicts):
        candidates = by_ticker.get(ticker, [])
        if not candidates:
            continue
        row, source = candidates[0]
        record_id = deterministic_issuer_record_id(
            provider=KRX_KIND_PROVIDER,
            snapshot_id=workspace.snapshot_id,
            market=row.market_id,
            ticker=ticker,
            issuer_source_id=row.issuer_code,
        )
        records.append(ExternalSecurityIssuerRecord(
            issuer_record_id=record_id,
            security_name_raw=row.issuer_name,
            security_ticker=ticker,
            security_market=row.market_name,
            security_source_id=ticker,
            issuer_name_raw=row.issuer_name,
            issuer_source_id=row.issuer_code,
            relation_type="SECURITY_ISSUED_BY",
            effective_date=cutoff,
            source_provider=KRX_KIND_PROVIDER,
            source_url=source.source_url,
            source_record_id=source.source_record_id,
            retrieved_at=source.retrieved_at,
            snapshot_id=workspace.snapshot_id,
            trust_tier=int(SourceTrustTier.AUTHORITATIVE),
            security_identity_status=IssuerIdentityStatus.RESOLVED,
            issuer_identity_status=IssuerIdentityStatus.RESOLVED,
            relation_validation_status=IssuerIdentityStatus.RESOLVED,
        ))

    workspace.write_source_records(category="issuers", records=sources)
    workspace.write_normalized_jsonl(
        category="issuers", filename="security_issuers.jsonl",
        schema_version=EXTERNAL_SECURITY_ISSUER_SCHEMA,
        canonical_rows=(record.canonical_json() for record in records),
    )
    unresolved = tuple(sorted(scoped_tickers - set(by_ticker) - conflicts))
    result = KrxIssuerSnapshotResult(
        scoped_security_count=len(scoped_tickers),
        records=tuple(records),
        unresolved_tickers=unresolved,
        conflicting_tickers=tuple(sorted(conflicts)),
        source_record_count=len(sources),
        artifact_count=len(workspace.manifest.raw_artifacts),
    )
    quality = SourceQualityReport(
        provider=KRX_KIND_PROVIDER,
        trust_tier=SourceTrustTier.AUTHORITATIVE,
        access_method="official exact-date KIND listed-company result",
        data_types=[ContentType.HTML],
        refresh_behavior="immutable snapshot keyed by requested cutoff date",
        identity_fields_available=["representative_ticker", "isurCd"],
        timestamps_available=["effective_date", "retrieved_at", "listing_date"],
        known_limitations=[
            "company-grain output does not map non-representative preferred-share tickers",
            "DART corporation code and ISIN are not present in this source contract",
        ],
        terms_and_access_constraints=["official public KRX KIND endpoint"],
        attempted_sources=len(_MARKETS),
        successful_sources=len(sources),
        failed_sources=0,
    )
    workspace.finalize(
        SnapshotStatus.READY,
        validation={
            "schema_version": KRX_KIND_ISSUER_SNAPSHOT_SCHEMA,
            "cutoff_exact": True,
            "scoped_security_count": len(scoped_tickers),
            "resolved_source_records": len(records),
            "unresolved_tickers": list(unresolved),
            "conflicting_tickers": sorted(conflicts),
            "post_cutoff_records": 0,
            "canonical_v2_writes": 0,
        },
        quality_reports=[quality],
    )
    return result


def load_issuer_records(snapshot_root: Path) -> tuple[ExternalSecurityIssuerRecord, ...]:
    path = snapshot_root / "issuers" / "normalized" / "security_issuers.jsonl"
    return tuple(
        ExternalSecurityIssuerRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )


def semantic_checksum(records: tuple[ExternalSecurityIssuerRecord, ...]) -> str:
    return hashlib.sha256(
        "\n".join(sorted(record.canonical_json() for record in records)).encode()
    ).hexdigest()
