"""Build and load the evidence-backed KODEX long-only READY scope.

The full crawl remains immutable and PARTIAL.  This module writes only a
logical selection index and a scope manifest beneath ``scopes/``; raw source
artifacts and normalized holdings are always read from the parent snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.data.holdings import TrustedHoldingsSnapshot
from app.data.holdings_coverage import KODEX_READY_SCOPE
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.kodex_production import (
    ProductCrawlResult,
    ProductCrawlStatus,
)
from app.external_data.holdings.models import ExternalHolding, IdentityStatus
from app.external_data.manifest import ExternalSnapshotManifest, SnapshotStatus
from app.external_data.models import ExternalSourceRecord, QualityStatus


KODEX_SCOPE_SCHEMA = "external-kodex-holdings-scope-v1"
KODEX_SCOPE_VERSION = "kodex-long-only-compatible-20260824-v1"
_KRX_EQUITY_TICKER = re.compile(r"\d{6}\Z")
_DERIVATIVE_SOURCE_ID = re.compile(r"KR4[A-Z0-9]{9}\Z")


class ProductEligibilityStatus(StrEnum):
    READY_LONG_ONLY = "READY_LONG_ONLY"
    UNSUPPORTED_POSITION_SEMANTICS = "UNSUPPORTED_POSITION_SEMANTICS"
    INCOMPLETE_SOURCE_RESPONSE = "INCOMPLETE_SOURCE_RESPONSE"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    OTHER_BLOCKED = "OTHER_BLOCKED"


class ProductEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_source_id: str
    product_name: str | None = None
    product_ticker: str | None = None
    product_isin: str | None = None
    status: ProductEligibilityStatus
    portfolio_row_count: int = 0
    eligible_security_row_count: int = 0
    non_security_row_count: int = 0
    reasons: list[str] = Field(default_factory=list)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )


class ScopedArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    sha256: str
    row_count: int | None = None


class KodexReadyScopeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = KODEX_SCOPE_SCHEMA
    scope_version: str = KODEX_SCOPE_VERSION
    scope: str = KODEX_READY_SCOPE
    status: str = "READY"
    source_snapshot_id: str
    source_snapshot_status: str
    source_manifest: ScopedArtifactReference
    data_cutoff_date: date
    full_product_count: int
    ready_product_count: int
    blocked_product_count: int
    classification_counts: dict[str, int]
    portfolio_row_count: int
    eligible_security_row_count: int
    non_security_row_count: int
    unique_security_source_ids: int
    product_eligibility: ScopedArtifactReference
    holding_selection: ScopedArtifactReference
    source_holdings: ScopedArtifactReference
    source_evidence_links: ScopedArtifactReference
    source_records: ScopedArtifactReference
    referenced_source_record_count: int
    referenced_raw_artifact_count: int
    referenced_raw_sha256_set_checksum: str
    eligibility_contract: list[str]
    paging_contract: str
    validation: dict[str, bool]

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"), ensure_ascii=False,
                sort_keys=True, indent=2,
            ) + "\n"
        ).encode()


def build_kodex_ready_scope(snapshot_root: Path) -> tuple[Path, KodexReadyScopeManifest]:
    """Create an idempotent logical scope without copying source evidence."""

    source_manifest_path = snapshot_root / "manifest.json"
    source_manifest = ExternalSnapshotManifest.model_validate_json(
        source_manifest_path.read_text(encoding="utf-8")
    )
    if source_manifest.status is not SnapshotStatus.PARTIAL:
        raise ValueError("KODEX full-universe source snapshot must remain PARTIAL")
    if source_manifest.data_cutoff_date != DATA_CUTOFF_DATE:
        raise ValueError("KODEX source cutoff does not match 2026-08-24")

    paths = _source_paths(snapshot_root)
    crawl_results = [
        ProductCrawlResult.model_validate_json(line)
        for line in paths["crawl_results"].read_text(encoding="utf-8").splitlines()
        if line
    ]
    holdings = _read_jsonl(paths["holdings"])
    holding_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in holdings:
        holding_by_product[str(row["product_source_id"])].append(row)
    source_records = [
        ExternalSourceRecord.model_validate_json(line)
        for line in paths["source_records"].read_text(encoding="utf-8").splitlines()
        if line
    ]
    sources_by_product: dict[str, list[ExternalSourceRecord]] = defaultdict(list)
    for record in source_records:
        product_id = record.metadata.get("product_source_id")
        if isinstance(product_id, str):
            sources_by_product[product_id].append(record)

    classifications = tuple(
        _classify_product(
            result,
            holding_by_product.get(result.product_source_id, []),
            sources_by_product.get(result.product_source_id, []),
            snapshot_root,
        )
        for result in sorted(crawl_results, key=lambda item: item.product_source_id)
    )
    if len(classifications) != source_manifest.normalized_row_counts.get(
        "external-kodex-catalog-v1", -1
    ):
        raise ValueError("product eligibility does not cover the full KODEX catalog")

    ready_ids = {
        item.product_source_id for item in classifications
        if item.status is ProductEligibilityStatus.READY_LONG_ONLY
    }
    ready_rows = sorted(
        (row for row in holdings if str(row["product_source_id"]) in ready_ids),
        key=lambda row: str(row["holding_record_id"]),
    )
    selection = [
        {
            "holding_record_id": row["holding_record_id"],
            "canonical_eligible": row["constituent_identity_status"] != "NON_SECURITY",
        }
        for row in ready_rows
    ]

    evidence_links = _read_jsonl(paths["evidence_links"])
    ready_holding_ids = {str(row["holding_record_id"]) for row in ready_rows}
    selected_links = [
        link for link in evidence_links
        if str(link["holding_record_id"]) in ready_holding_ids
    ]
    referenced_source_ids = {str(link["source_record_id"]) for link in selected_links}
    selected_sources = [
        source for source in source_records
        if source.source_record_id in referenced_source_ids
    ]
    if referenced_source_ids != {item.source_record_id for item in selected_sources}:
        raise ValueError("READY scope evidence references an absent SourceRecord")
    raw_hashes = sorted({item.raw_content_hash for item in selected_sources})
    for source in selected_sources:
        raw_path = snapshot_root / source.raw_artifact_path
        if not raw_path.is_file() or _sha256(raw_path) != source.raw_content_hash:
            raise ValueError("READY scope references invalid raw evidence")

    scope_dir = snapshot_root / "scopes" / KODEX_SCOPE_VERSION
    scope_dir.mkdir(parents=True, exist_ok=True)
    eligibility_ref = _write_jsonl(
        scope_dir / "product_eligibility.jsonl",
        [json.loads(item.canonical_json()) for item in classifications],
        root=snapshot_root,
    )
    selection_ref = _write_jsonl(
        scope_dir / "holding_selection.jsonl", selection, root=snapshot_root,
    )
    counts = Counter(item.status.value for item in classifications)
    source_outputs = {item.relative_path: item for item in source_manifest.normalized_outputs}
    security_rows = [
        row for row in ready_rows
        if row["constituent_identity_status"] != "NON_SECURITY"
    ]
    manifest = KodexReadyScopeManifest(
        source_snapshot_id=source_manifest.snapshot_id,
        source_snapshot_status=source_manifest.status.value,
        source_manifest=_reference(source_manifest_path, snapshot_root),
        data_cutoff_date=DATA_CUTOFF_DATE,
        full_product_count=len(classifications),
        ready_product_count=len(ready_ids),
        blocked_product_count=len(classifications) - len(ready_ids),
        classification_counts=dict(sorted(counts.items())),
        portfolio_row_count=len(ready_rows),
        eligible_security_row_count=len(security_rows),
        non_security_row_count=len(ready_rows) - len(security_rows),
        unique_security_source_ids=len({row["constituent_source_id"] for row in security_rows}),
        product_eligibility=eligibility_ref,
        holding_selection=selection_ref,
        source_holdings=_output_reference(source_outputs, paths["holdings"], snapshot_root),
        source_evidence_links=_output_reference(
            source_outputs, paths["evidence_links"], snapshot_root
        ),
        source_records=_output_reference(
            source_outputs, paths["source_records"], snapshot_root
        ),
        referenced_source_record_count=len(referenced_source_ids),
        referenced_raw_artifact_count=len(raw_hashes),
        referenced_raw_sha256_set_checksum=hashlib.sha256(
            "\n".join(raw_hashes).encode()
        ).hexdigest(),
        eligibility_contract=[
            "the provider response is complete and effective on or before the cutoff",
            "every Security position has a six-digit KRX identifier supported by C2",
            "every Security position has verified percent-point weight semantics",
            "quantity and evaluated value are non-negative where present",
            "no KR4 derivative or other unsupported instrument identifier is present",
            "explicit KRD/원화예금 cash rows are retained as non-Security evidence",
            "one unsupported row blocks the entire product; rows are never silently dropped",
        ],
        paging_contract=(
            "the official product-pdf client uses one date-qualified request and exposes no "
            "documented paging parameter; 1000/1264 remains INCOMPLETE_SOURCE_RESPONSE"
        ),
        validation={
            "full_snapshot_unchanged": True,
            "all_catalog_products_classified": True,
            "ready_products_have_complete_portfolios": True,
            "source_checksums_verified": True,
            "cutoff_verified": all(
                date.fromisoformat(str(row["effective_date"])) <= DATA_CUTOFF_DATE
                for row in ready_rows
            ),
            "raw_evidence_not_duplicated": True,
        },
    )
    manifest_path = scope_dir / "manifest.json"
    _atomic_write(manifest_path, manifest.canonical_bytes())
    return manifest_path, manifest


def load_trusted_scope(
    snapshot_root: Path,
    *,
    canonical_snapshot_id: str,
) -> TrustedHoldingsSnapshot:
    """Verify and hydrate the logical READY scope for canonical integration."""

    manifest_path = snapshot_root / "scopes" / KODEX_SCOPE_VERSION / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = KodexReadyScopeManifest.model_validate_json(manifest_bytes)
    if manifest.status != "READY" or manifest.scope != KODEX_READY_SCOPE:
        raise ValueError("KODEX holdings scope is not READY")
    for reference in (
        manifest.source_manifest,
        manifest.product_eligibility,
        manifest.holding_selection,
        manifest.source_holdings,
        manifest.source_evidence_links,
        manifest.source_records,
    ):
        path = snapshot_root / reference.relative_path
        if not path.is_file() or _sha256(path) != reference.sha256:
            raise ValueError("KODEX READY scope reference checksum mismatch")

    selection = {
        str(item["holding_record_id"]): bool(item["canonical_eligible"])
        for item in _read_jsonl(snapshot_root / manifest.holding_selection.relative_path)
    }
    all_holdings = {
        str(item["holding_record_id"]): item
        for item in _read_jsonl(snapshot_root / manifest.source_holdings.relative_path)
    }
    links: dict[str, list[str]] = defaultdict(list)
    for item in _read_jsonl(snapshot_root / manifest.source_evidence_links.relative_path):
        holding_id = str(item["holding_record_id"])
        if holding_id in selection:
            links[holding_id].append(str(item["source_record_id"]))
    all_sources = {
        item.source_record_id: item
        for item in (
            ExternalSourceRecord.model_validate_json(line)
            for line in (snapshot_root / manifest.source_records.relative_path)
            .read_text(encoding="utf-8").splitlines()
            if line
        )
    }
    selected_source_ids = {source_id for values in links.values() for source_id in values}
    selected_sources = tuple(
        all_sources[source_id] for source_id in sorted(selected_source_ids)
    )
    hydrated: list[ExternalHolding] = []
    for holding_id in sorted(selection):
        source_ids = sorted(links.get(holding_id, []))
        if not source_ids:
            raise ValueError("selected holding has no source evidence")
        valid_sources = [
            all_sources[source_id] for source_id in source_ids
            if all_sources[source_id].quality_status is QualityStatus.VALID
        ]
        if not valid_sources:
            raise ValueError("selected holding has no VALID source evidence")
        source = valid_sources[0]
        payload = dict(all_holdings[holding_id])
        payload.update({
            "retrieved_at": source.retrieved_at,
            "source_record_id": source.source_record_id,
            "source_provider": source.source_provider,
            "source_url": source.source_url,
            "source_trust_tier": source.source_trust_tier,
            "snapshot_id": manifest.scope_version,
        })
        hydrated.append(ExternalHolding.model_validate(payload))

    return TrustedHoldingsSnapshot(
        external_snapshot_id=manifest.scope_version,
        canonical_snapshot_id=canonical_snapshot_id,
        manifest_schema_version=manifest.schema_version,
        manifest_status=manifest.status,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        manifest_json=manifest.model_dump(mode="json"),
        data_cutoff_date=manifest.data_cutoff_date,
        artifact_root=snapshot_root,
        source_records=selected_sources,
        holdings=tuple(hydrated),
        manifest_path=manifest_path,
        source_snapshot_id=manifest.source_snapshot_id,
    )


def _classify_product(
    result: ProductCrawlResult,
    holdings: list[dict[str, Any]],
    sources: list[ExternalSourceRecord],
    snapshot_root: Path,
) -> ProductEligibility:
    base = {
        "product_source_id": result.product_source_id,
        "product_name": result.product_name,
        "product_ticker": result.product_ticker,
        "product_isin": result.product_isin,
    }
    if result.status is ProductCrawlStatus.IDENTITY_UNRESOLVED:
        return ProductEligibility(
            **base, status=ProductEligibilityStatus.IDENTITY_UNRESOLVED,
            reasons=[result.reason or "catalog identity unresolved"],
        )
    if result.status is ProductCrawlStatus.PARSE_FAILED:
        reason = _failed_response_reason(sources, snapshot_root)
        status = (
            ProductEligibilityStatus.INCOMPLETE_SOURCE_RESPONSE
            if reason == "source response row count is incomplete"
            else ProductEligibilityStatus.UNSUPPORTED_POSITION_SEMANTICS
            if reason.startswith("unsupported ")
            else ProductEligibilityStatus.OTHER_BLOCKED
        )
        return ProductEligibility(**base, status=status, reasons=[reason])
    if result.status is not ProductCrawlStatus.SUCCESS:
        return ProductEligibility(
            **base, status=ProductEligibilityStatus.OTHER_BLOCKED,
            reasons=[result.reason or result.status.value],
        )
    if len(holdings) != result.holding_count:
        return ProductEligibility(
            **base, status=ProductEligibilityStatus.INCOMPLETE_SOURCE_RESPONSE,
            portfolio_row_count=len(holdings),
            reasons=["normalized portfolio row count does not match crawl result"],
        )

    unsupported: list[str] = []
    unresolved: list[str] = []
    invalid: list[str] = []
    security_count = 0
    cash_count = 0
    for row in holdings:
        source_id = str(row.get("constituent_source_id") or "")
        status = str(row["constituent_identity_status"])
        if status == IdentityStatus.NON_SECURITY.value:
            cash_count += 1
            if not (source_id.startswith("KRD") or row["constituent_name_raw"] == "원화예금"):
                invalid.append("unreviewed non-Security classification")
            continue
        security_count += 1
        if _DERIVATIVE_SOURCE_ID.fullmatch(source_id):
            unsupported.append(source_id)
            continue
        if not _KRX_EQUITY_TICKER.fullmatch(source_id):
            unresolved.append(source_id or "<missing>")
            continue
        if (
            row.get("weight_normalized") is None
            or row.get("weight_unit") != "PERCENT_OF_NON_CASH_ASSETS"
            or row.get("weight_scale") != "PERCENT_POINTS"
        ):
            invalid.append("Security position lacks verified weight semantics")
        for key in ("quantity_normalized", "market_value_normalized"):
            value = row.get(key)
            if value is not None and Decimal(str(value)) < 0:
                unsupported.append(f"negative {key}")
        if date.fromisoformat(str(row["effective_date"])) > DATA_CUTOFF_DATE:
            invalid.append("post-cutoff holding")

    common = {
        **base,
        "portfolio_row_count": len(holdings),
        "eligible_security_row_count": security_count,
        "non_security_row_count": cash_count,
    }
    if unsupported:
        return ProductEligibility(
            **common,
            status=ProductEligibilityStatus.UNSUPPORTED_POSITION_SEMANTICS,
            reasons=["unsupported derivative/short instrument identity: " + ",".join(sorted(set(unsupported)))],
        )
    if unresolved:
        return ProductEligibility(
            **common, status=ProductEligibilityStatus.IDENTITY_UNRESOLVED,
            reasons=[
                "constituent identity is outside the current C2 KRX Equity contract: "
                + ",".join(sorted(set(unresolved))[:10])
            ],
        )
    if invalid:
        return ProductEligibility(
            **common, status=ProductEligibilityStatus.OTHER_BLOCKED,
            reasons=sorted(set(invalid)),
        )
    return ProductEligibility(
        **common, status=ProductEligibilityStatus.READY_LONG_ONLY,
    )


def _failed_response_reason(
    sources: list[ExternalSourceRecord], snapshot_root: Path,
) -> str:
    for source in sorted(sources, key=lambda item: item.source_record_id):
        path = snapshot_root / source.raw_artifact_path
        if not path.is_file():
            continue
        try:
            pdf = json.loads(path.read_text(encoding="utf-8"))["pdf"]
            rows = pdf["list"]
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if int(pdf["totalCnt"]) != len(rows) or int(pdf["nowCnt"]) != len(rows):
            return "source response row count is incomplete"
        negative_quantity = any(_negative(row.get("applyQ")) for row in rows)
        negative_value = any(_negative(row.get("evalA")) for row in rows)
        excessive_weight = any(_greater_than_100(row.get("ratio")) for row in rows)
        reasons = []
        if negative_quantity:
            reasons.append("negative quantity")
        if negative_value:
            reasons.append("negative evaluated value")
        if excessive_weight:
            reasons.append("gross/position weight above 100 percent")
        if reasons:
            return "unsupported " + ", ".join(reasons)
    return "provider response did not satisfy the reviewed semantic contract"


def _negative(value: Any) -> bool:
    try:
        return value is not None and Decimal(str(value).replace(",", "")) < 0
    except InvalidOperation:
        return False


def _greater_than_100(value: Any) -> bool:
    try:
        return value is not None and Decimal(str(value).replace(",", "")) > 100
    except InvalidOperation:
        return False


def _source_paths(root: Path) -> dict[str, Path]:
    return {
        "crawl_results": root / "holdings/normalized/crawl_results.jsonl",
        "holdings": root / "holdings/normalized/holdings.jsonl",
        "evidence_links": root / "holdings/normalized/holding_evidence_links.jsonl",
        "source_records": root / "holdings/normalized/source_records.jsonl",
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _reference(path: Path, root: Path, *, rows: int | None = None) -> ScopedArtifactReference:
    return ScopedArtifactReference(
        relative_path=path.relative_to(root).as_posix(), sha256=_sha256(path), row_count=rows,
    )


def _output_reference(outputs, path: Path, root: Path) -> ScopedArtifactReference:
    relative = path.relative_to(root).as_posix()
    output = outputs.get(relative)
    if output is None or output.sha256 != _sha256(path):
        raise ValueError(f"source manifest checksum is absent/mismatched: {relative}")
    return _reference(path, root, rows=output.row_count)


def _write_jsonl(path: Path, rows: list[dict[str, Any]], *, root: Path) -> ScopedArtifactReference:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in sorted(rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    ).encode()
    _atomic_write(path, payload)
    return _reference(path, root, rows=len(rows))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
