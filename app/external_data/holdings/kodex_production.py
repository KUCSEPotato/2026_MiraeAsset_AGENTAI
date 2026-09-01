from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.external_data.holdings.catalog import (
    CatalogResolutionStatus,
    resolve_catalog_against_pref01,
)
from app.external_data.holdings.contract import PostCutoffHoldingError
from app.external_data.holdings.providers.kodex import (
    KodexHoldingsAdapter,
    KodexProduct,
    KodexSchemaError,
)
from app.external_data.holdings.providers.kodex_catalog import KodexCatalogAdapter
from app.external_data.http import TrustedHttpClient
from app.external_data.manifest import SnapshotWorkspace


KODEX_CRAWL_RESULT_SCHEMA = "external-kodex-crawl-result-v1"
MAX_ACCOUNTED_FETCH_FAILURE_RATE = 0.05
MAX_CONSECUTIVE_FETCH_FAILURES = 3


class ProductCrawlStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FETCH_FAILED = "FETCH_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    CUTOFF_UNVERIFIED = "CUTOFF_UNVERIFIED"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    NO_HOLDINGS = "NO_HOLDINGS"


class ProductCrawlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = KODEX_CRAWL_RESULT_SCHEMA
    product_source_id: str
    product_name: str | None
    product_ticker: str | None
    product_isin: str | None
    status: ProductCrawlStatus
    holding_count: int = 0
    effective_date: date | None = None
    reason: str | None = None

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ProductionCrawlResult:
    catalog_count: int
    catalog_with_fid: int
    catalog_with_ticker: int
    catalog_with_isin: int
    matched_by_isin: int
    matched_by_ticker: int
    ambiguous: int
    unmatched: int
    eligible_products: int
    first_results: tuple[ProductCrawlResult, ...]
    second_results: tuple[ProductCrawlResult, ...]
    first_holding_count: int
    second_holding_count: int
    first_semantic_checksum: str | None
    second_semantic_checksum: str | None
    holding_ids_stable: bool
    semantic_checksum_stable: bool
    provenance_valid: bool
    cutoff_valid: bool
    ready: bool
    ready_reasons: tuple[str, ...]

    @property
    def status_counts(self) -> Counter[ProductCrawlStatus]:
        return Counter(item.status for item in self.second_results)


async def run_kodex_production_crawl(
    client: TrustedHttpClient,
    workspace: SnapshotWorkspace,
    *,
    pref01_data: Path,
    requested_date: date,
    selected_product_ids: frozenset[str] = frozenset(),
    verify_rerun: bool = True,
) -> ProductionCrawlResult:
    catalog = await KodexCatalogAdapter(client, workspace).discover()
    resolution = resolve_catalog_against_pref01(catalog.products, pref01_data)
    workspace.write_normalized_jsonl(
        category="catalog",
        filename="pref01_resolution.jsonl",
        schema_version="external-kodex-pref01-resolution-v1",
        canonical_rows=[item.canonical_json() for item in resolution.entries],
    )

    matched = resolution.matched_products
    if selected_product_ids:
        known = {item.source_id for item in catalog.products}
        missing = selected_product_ids - known
        if missing:
            raise ValueError(f"selected KODEX fIds not present in catalog: {sorted(missing)}")
        matched = tuple(item for item in matched if item.source_id in selected_product_ids)

    unresolved_results = tuple(
        ProductCrawlResult(
            product_source_id=item.catalog_source_id,
            product_name=item.catalog_name,
            product_ticker=item.catalog_ticker,
            product_isin=item.catalog_isin,
            status=ProductCrawlStatus.IDENTITY_UNRESOLVED,
            reason=item.status.value,
        )
        for item in resolution.entries
        if item.status is not CatalogResolutionStatus.MATCHED
        and (not selected_product_ids or item.catalog_source_id in selected_product_ids)
    )
    adapter = KodexHoldingsAdapter(client, workspace)
    first_resolved, first_complete = await _crawl_pass(
        adapter, matched, requested_date, workspace=workspace, pass_number=1,
        infer_existing_holdings=True,
    )
    first_results = tuple(sorted(
        (*first_resolved, *unresolved_results), key=lambda item: item.product_source_id,
    ))
    _write_results(workspace, first_results)
    first_ids, first_checksum = _holding_state(workspace)

    second_results = first_results
    second_complete = False
    if verify_rerun and first_complete:
        first_by_id = {item.product_source_id: item for item in first_resolved}
        rerun_products = tuple(
            item for item in matched
            if first_by_id[item.source_id].status is ProductCrawlStatus.SUCCESS
        )
        rerun_results, second_complete = await _crawl_pass(
            adapter, rerun_products, requested_date, workspace=workspace, pass_number=2,
            infer_existing_holdings=False,
        )
        rerun_by_id = {item.product_source_id: item for item in rerun_results}
        second_resolved = tuple(
            rerun_by_id.get(item.source_id, first_by_id[item.source_id])
            for item in matched
        )
        second_results = tuple(sorted(
            (*second_resolved, *unresolved_results), key=lambda item: item.product_source_id,
        ))
        _write_results(workspace, second_results)
    second_ids, second_checksum = _holding_state(workspace)
    provenance_valid = _validate_provenance(workspace, second_ids)
    cutoff_valid = _validate_cutoff(workspace, requested_date)
    stable_ids = first_ids == second_ids
    stable_checksum = first_checksum == second_checksum
    ready, reasons = _ready_policy(
        results=second_results,
        eligible_count=len(matched),
        stable_ids=stable_ids,
        stable_checksum=stable_checksum,
        provenance_valid=provenance_valid,
        cutoff_valid=cutoff_valid,
        rerun_verified=verify_rerun and first_complete and second_complete,
    )
    return ProductionCrawlResult(
        catalog_count=len(catalog.products),
        catalog_with_fid=sum(bool(item.source_id) for item in catalog.products),
        catalog_with_ticker=sum(bool(item.ticker) for item in catalog.products),
        catalog_with_isin=sum(bool(item.isin) for item in catalog.products),
        matched_by_isin=resolution.matched_by_isin,
        matched_by_ticker=resolution.matched_by_ticker,
        ambiguous=resolution.ambiguous,
        unmatched=resolution.unmatched,
        eligible_products=len(matched),
        first_results=first_results,
        second_results=second_results,
        first_holding_count=len(first_ids),
        second_holding_count=len(second_ids),
        first_semantic_checksum=first_checksum,
        second_semantic_checksum=second_checksum,
        holding_ids_stable=stable_ids,
        semantic_checksum_stable=stable_checksum,
        provenance_valid=provenance_valid,
        cutoff_valid=cutoff_valid,
        ready=ready,
        ready_reasons=reasons,
    )


async def _crawl_pass(
    adapter: KodexHoldingsAdapter,
    products: tuple[KodexProduct, ...],
    requested_date: date,
    *,
    workspace: SnapshotWorkspace,
    pass_number: int,
    infer_existing_holdings: bool,
) -> tuple[tuple[ProductCrawlResult, ...], bool]:
    results = _load_pass_results(workspace, pass_number)
    if not results and infer_existing_holdings:
        results = _infer_successful_results(workspace, products)
        _write_pass_results(workspace, pass_number, results.values())
    consecutive_fetch_failures = 0
    for product in products:
        previous = results.get(product.source_id)
        if previous is not None and previous.status is not ProductCrawlStatus.FETCH_FAILED:
            continue
        try:
            acquired = await adapter.acquire(product, requested_date=requested_date)
        except PostCutoffHoldingError as exc:
            results[product.source_id] = _result(
                product, ProductCrawlStatus.CUTOFF_UNVERIFIED, reason=str(exc)
            )
            consecutive_fetch_failures = 0
            _write_pass_results(workspace, pass_number, results.values())
            continue
        except KodexSchemaError as exc:
            results[product.source_id] = _result(
                product, ProductCrawlStatus.PARSE_FAILED, reason=str(exc)
            )
            consecutive_fetch_failures = 0
            _write_pass_results(workspace, pass_number, results.values())
            continue
        if acquired.source_record is None:
            results[product.source_id] = _result(
                product,
                ProductCrawlStatus.FETCH_FAILED,
                reason=acquired.fetch.error_message or acquired.fetch.quality_status.value,
            )
            consecutive_fetch_failures += 1
        elif not acquired.holdings:
            results[product.source_id] = _result(
                product,
                ProductCrawlStatus.NO_HOLDINGS,
                effective_date=acquired.source_record.effective_date,
            )
            consecutive_fetch_failures = 0
        else:
            results[product.source_id] = _result(
                product,
                ProductCrawlStatus.SUCCESS,
                holding_count=len(acquired.holdings),
                effective_date=acquired.source_record.effective_date,
            )
            consecutive_fetch_failures = 0
        _write_pass_results(workspace, pass_number, results.values())
        if consecutive_fetch_failures >= MAX_CONSECUTIVE_FETCH_FAILURES:
            break
    ordered = tuple(sorted(results.values(), key=lambda item: item.product_source_id))
    complete = len(results) == len(products)
    return ordered, complete


def _load_pass_results(
    workspace: SnapshotWorkspace, pass_number: int,
) -> dict[str, ProductCrawlResult]:
    path = workspace.path / f"holdings/normalized/crawl_results_pass{pass_number}.jsonl"
    if not path.is_file():
        return {}
    return {
        item.product_source_id: item
        for item in (
            ProductCrawlResult.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        )
    }


def _infer_successful_results(
    workspace: SnapshotWorkspace, products: tuple[KodexProduct, ...],
) -> dict[str, ProductCrawlResult]:
    path = workspace.path / "holdings/normalized/holdings.jsonl"
    if not path.is_file():
        return {}
    counts: Counter[str] = Counter()
    effective_dates: dict[str, date] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = json.loads(line)
        source_id = str(row["product_source_id"])
        counts[source_id] += 1
        effective_dates[source_id] = date.fromisoformat(row["effective_date"])
    product_by_id = {item.source_id: item for item in products}
    return {
        source_id: _result(
            product_by_id[source_id],
            ProductCrawlStatus.SUCCESS,
            holding_count=count,
            effective_date=effective_dates[source_id],
        )
        for source_id, count in counts.items()
        if source_id in product_by_id
    }


def _write_pass_results(
    workspace: SnapshotWorkspace,
    pass_number: int,
    results,
) -> None:
    workspace.write_normalized_jsonl(
        category="holdings",
        filename=f"crawl_results_pass{pass_number}.jsonl",
        schema_version=KODEX_CRAWL_RESULT_SCHEMA,
        canonical_rows=[item.canonical_json() for item in results],
    )


def _result(
    product: KodexProduct,
    status: ProductCrawlStatus,
    *,
    holding_count: int = 0,
    effective_date: date | None = None,
    reason: str | None = None,
) -> ProductCrawlResult:
    return ProductCrawlResult(
        product_source_id=product.source_id,
        product_name=product.name,
        product_ticker=product.ticker,
        product_isin=product.isin,
        status=status,
        holding_count=holding_count,
        effective_date=effective_date,
        reason=reason,
    )


def _write_results(
    workspace: SnapshotWorkspace, results: tuple[ProductCrawlResult, ...],
) -> None:
    workspace.write_normalized_jsonl(
        category="holdings",
        filename="crawl_results.jsonl",
        schema_version=KODEX_CRAWL_RESULT_SCHEMA,
        canonical_rows=[item.canonical_json() for item in results],
    )


def _holding_state(workspace: SnapshotWorkspace) -> tuple[frozenset[str], str | None]:
    relative = "holdings/normalized/holdings.jsonl"
    entry = next(
        (item for item in workspace.manifest.normalized_outputs if item.relative_path == relative),
        None,
    )
    if entry is None:
        return frozenset(), None
    path = workspace.path / relative
    identities = frozenset(
        str(json.loads(line)["holding_record_id"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )
    return identities, entry.sha256


def _validate_provenance(
    workspace: SnapshotWorkspace, holding_ids: frozenset[str],
) -> bool:
    link_path = workspace.path / "holdings/normalized/holding_evidence_links.jsonl"
    if not holding_ids or not link_path.is_file():
        return False
    source_ids = workspace.source_record_ids
    supported: set[str] = set()
    for line in link_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        link = json.loads(line)
        holding_id = str(link["holding_record_id"])
        source_id = str(link["source_record_id"])
        if holding_id not in holding_ids or source_id not in source_ids:
            return False
        supported.add(holding_id)
    return supported == set(holding_ids)


def _validate_cutoff(workspace: SnapshotWorkspace, cutoff: date) -> bool:
    path = workspace.path / "holdings/normalized/holdings.jsonl"
    if not path.is_file():
        return False
    return all(
        date.fromisoformat(json.loads(line)["effective_date"]) <= cutoff
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )


def _ready_policy(
    *,
    results: tuple[ProductCrawlResult, ...],
    eligible_count: int,
    stable_ids: bool,
    stable_checksum: bool,
    provenance_valid: bool,
    cutoff_valid: bool,
    rerun_verified: bool,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    counts = Counter(item.status for item in results)
    terminal_resolved = sum(
        counts[item] for item in (
            ProductCrawlStatus.SUCCESS,
            ProductCrawlStatus.FETCH_FAILED,
            ProductCrawlStatus.PARSE_FAILED,
            ProductCrawlStatus.CUTOFF_UNVERIFIED,
            ProductCrawlStatus.NO_HOLDINGS,
        )
    )
    if terminal_resolved != eligible_count:
        reasons.append("not every eligible product has an accounted terminal status")
    if counts[ProductCrawlStatus.SUCCESS] == 0:
        reasons.append("no successfully normalized holdings")
    if counts[ProductCrawlStatus.PARSE_FAILED]:
        reasons.append("provider schema parse failures are present")
    if counts[ProductCrawlStatus.CUTOFF_UNVERIFIED]:
        reasons.append("cutoff-unverified products are present")
    fetch_rate = counts[ProductCrawlStatus.FETCH_FAILED] / eligible_count if eligible_count else 1.0
    if fetch_rate > MAX_ACCOUNTED_FETCH_FAILURE_RATE:
        reasons.append("accounted fetch failure rate exceeds 5%")
    if not rerun_verified or not stable_ids or not stable_checksum:
        reasons.append("semantic rerun stability is not verified")
    if not provenance_valid:
        reasons.append("normalized holding provenance coverage is invalid")
    if not cutoff_valid:
        reasons.append("normalized holding cutoff validation failed")
    return not reasons, tuple(reasons)
