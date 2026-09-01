from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.external_data.http import FetchResult, TrustedHttpClient
from app.external_data.manifest import NormalizedOutputEntry, SnapshotWorkspace
from app.external_data.models import (
    EXTERNAL_KODEX_CATALOG_SCHEMA,
    ContentType,
    CrawlFailure,
    ExternalSourceRecord,
    FailureStage,
    QualityStatus,
    SourceTrustTier,
    SourceType,
    deterministic_source_record_id,
)

from .kodex import KODEX_PROVIDER, _F_ID


KODEX_CATALOG_PARSER_VERSION = "kodex-product-catalog-json-v1"
KODEX_CATALOG_PAGE_SIZE = 20
KODEX_CATALOG_MAX_PAGES = 100
_TICKER = re.compile(r"[A-Za-z0-9]{6}\Z")


class KodexCatalogError(ValueError):
    pass


class KodexCatalogProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTERNAL_KODEX_CATALOG_SCHEMA
    source_id: str
    name: str
    ticker: str | None = None
    isin: str | None = None
    market: str = "KRX"
    product_url: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not _F_ID.fullmatch(value):
            raise ValueError("unsafe KODEX fId")
        return value

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class KodexCatalogResult:
    products: tuple[KodexCatalogProduct, ...]
    source_records: tuple[ExternalSourceRecord, ...]
    normalized_output: NormalizedOutputEntry
    total_reported: int


class KodexCatalogAdapter:
    """Deterministic parser for the official, paginated KODEX product API."""

    def __init__(self, client: TrustedHttpClient, workspace: SnapshotWorkspace) -> None:
        self._client = client
        self._workspace = workspace

    @staticmethod
    def source_url(page_no: int) -> str:
        if not 1 <= page_no <= KODEX_CATALOG_MAX_PAGES:
            raise ValueError("catalog page is outside the bounded range")
        return (
            "https://www.samsungfund.com/api/v1/kodex/product.do"
            "?srchTerm=w&ordrSort=ASC&ordrColm=F_ID&pageNo=" + str(page_no)
        )

    async def discover(self) -> KodexCatalogResult:
        existing = self._load_existing_catalog()
        if existing is not None:
            return existing
        products: dict[str, KodexCatalogProduct] = {}
        records: list[ExternalSourceRecord] = []
        total_reported: int | None = None
        expected_pages: int | None = None
        page_no = 1
        while expected_pages is None or page_no <= expected_pages:
            fetch = await self._fetch_page(page_no)
            if fetch.content is None or fetch.content_hash is None:
                raise KodexCatalogError(f"KODEX catalog page {page_no} fetch failed")
            artifact = self._workspace.preserve_raw(
                category="catalog",
                content=fetch.content,
                suffix="json",
                normalized_url=fetch.normalized_url,
                content_type=ContentType.JSON.value,
            )
            page, page_total = self._parse_page(fetch.content)
            if total_reported is None:
                total_reported = page_total
                expected_pages = math.ceil(page_total / KODEX_CATALOG_PAGE_SIZE)
                if expected_pages > KODEX_CATALOG_MAX_PAGES:
                    raise KodexCatalogError("catalog exceeds bounded page contract")
            elif page_total != total_reported:
                raise KodexCatalogError("catalog total changed during paginated discovery")
            record = self._source_record(fetch, artifact.relative_path, page_no, page_total)
            self._workspace.write_source_records(category="catalog", records=[record])
            records.append(record)
            for product in page:
                previous = products.get(product.source_id)
                if previous is not None and previous != product:
                    raise KodexCatalogError("duplicate fId has conflicting stable catalog identity")
                products[product.source_id] = product
            page_no += 1
        if total_reported is None or len(products) != total_reported:
            raise KodexCatalogError(
                f"catalog coverage mismatch: reported={total_reported}, unique={len(products)}"
            )
        ordered = tuple(sorted(products.values(), key=lambda item: item.source_id))
        output = self._workspace.write_normalized_jsonl(
            category="catalog",
            filename="products.jsonl",
            schema_version=EXTERNAL_KODEX_CATALOG_SCHEMA,
            canonical_rows=[item.canonical_json() for item in ordered],
        )
        return KodexCatalogResult(ordered, tuple(records), output, total_reported)

    def _load_existing_catalog(self) -> KodexCatalogResult | None:
        """Reuse a complete normalized catalog when resuming an unfinished run."""

        relative = "catalog/normalized/products.jsonl"
        entry = next(
            (
                item for item in self._workspace.manifest.normalized_outputs
                if item.relative_path == relative
            ),
            None,
        )
        path = self._workspace.path / relative
        if entry is None or not path.is_file():
            return None
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry.sha256:
            raise KodexCatalogError("existing normalized catalog checksum mismatch")
        products = tuple(sorted(
            (
                KodexCatalogProduct.model_validate_json(line)
                for line in payload.decode("utf-8").splitlines()
                if line
            ),
            key=lambda item: item.source_id,
        ))
        if (
            len(products) != entry.row_count
            or len({item.source_id for item in products}) != len(products)
        ):
            raise KodexCatalogError("existing normalized catalog is incomplete or duplicated")
        return KodexCatalogResult(products, (), entry, len(products))

    async def _fetch_page(self, page_no: int) -> FetchResult:
        url = self.source_url(page_no)
        self._workspace.add_source(KODEX_PROVIDER, url)
        fetch = await self._client.fetch(url)
        if fetch.content is None:
            self._workspace.add_failure(CrawlFailure(
                source_url=fetch.requested_url,
                normalized_url=fetch.normalized_url,
                source_provider=KODEX_PROVIDER,
                failure_stage=FailureStage.FETCH,
                quality_status=fetch.quality_status,
                error_type=fetch.error_type or "CatalogFetchError",
                error_message=fetch.error_message or "catalog page fetch failed",
                retry_count=max(fetch.attempts - 1, 0),
            ))
        return fetch

    @staticmethod
    def _parse_page(content: bytes) -> tuple[list[KodexCatalogProduct], int]:
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KodexCatalogError("catalog response is not valid JSON") from exc
        if not isinstance(payload, list) or not payload:
            raise KodexCatalogError("catalog page must be a non-empty JSON array")
        products: list[KodexCatalogProduct] = []
        totals: set[int] = set()
        for item in payload:
            if not isinstance(item, dict):
                raise KodexCatalogError("catalog row must be an object")
            try:
                source_id = str(item["fId"]).strip()
                name = str(item["fNm"]).strip()
                ticker_raw = item.get("stkTicker")
                total = int(item["totalCnt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise KodexCatalogError("catalog required field is missing or invalid") from exc
            ticker = str(ticker_raw).strip().upper() if ticker_raw is not None else None
            if not name or (ticker is not None and not _TICKER.fullmatch(ticker)):
                raise KodexCatalogError("catalog stable identity field is invalid")
            totals.add(total)
            products.append(KodexCatalogProduct(
                source_id=source_id,
                name=name,
                ticker=ticker,
                isin=None,
                market="KRX",
                product_url=(
                    "https://www.samsungfund.com/etf/product/view.do?id=" + source_id
                ),
            ))
        if len(totals) != 1:
            raise KodexCatalogError("catalog page reports inconsistent totalCnt")
        return products, totals.pop()

    def _source_record(
        self, fetch: FetchResult, artifact_path: str, page_no: int, total: int,
    ) -> ExternalSourceRecord:
        source_record_id = deterministic_source_record_id(
            source_provider=KODEX_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            normalized_url=fetch.normalized_url,
            raw_content_hash=fetch.content_hash or "",
        )
        return ExternalSourceRecord(
            source_record_id=source_record_id,
            source_provider=KODEX_PROVIDER,
            source_type=SourceType.ASSET_MANAGER,
            source_trust_tier=SourceTrustTier.AUTHORITATIVE,
            source_url=fetch.requested_url,
            normalized_url=fetch.normalized_url,
            retrieved_at=fetch.retrieved_at,
            published_at=None,
            effective_date=None,
            source_title=f"KODEX ETF Product Catalog page {page_no}",
            content_type=ContentType.JSON,
            http_status=fetch.status_code,
            raw_content_hash=fetch.content_hash or "",
            parser_version=KODEX_CATALOG_PARSER_VERSION,
            crawler_version=self._workspace.manifest.crawler_version,
            snapshot_id=self._workspace.snapshot_id,
            quality_status=QualityStatus.VALID,
            raw_artifact_path=artifact_path,
            etag=fetch.etag,
            last_modified=fetch.last_modified,
            metadata={"catalog_page": page_no, "catalog_total": total},
        )
