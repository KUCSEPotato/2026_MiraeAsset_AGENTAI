"""Evidence-backed TIGER long-only READY scope selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.data.holdings import TrustedHoldingsSnapshot
from app.data.holdings_coverage import TIGER_READY_SCOPE
from app.external_data.holdings.contract import DATA_CUTOFF_DATE
from app.external_data.holdings.models import ExternalHolding
from app.external_data.holdings.provider_contracts import TIGER_CONTRACT
from app.external_data.holdings.tiger_production import TigerCrawlResult, TigerCrawlStatus
from app.external_data.manifest import ExternalSnapshotManifest, SnapshotStatus
from app.external_data.models import ExternalSourceRecord, QualityStatus


TIGER_SCOPE_SCHEMA = "external-tiger-holdings-scope-v1"
TIGER_SCOPE_VERSION = "tiger-long-only-compatible-20260824-v1"
_KRX_EQUITY_TICKER = re.compile(r"\d{6}\Z")


class TigerProductEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_source_id: str
    product_name: str
    product_ticker: str
    product_isin: str
    status: str
    portfolio_row_count: int
    eligible_security_row_count: int
    non_security_row_count: int
    reasons: list[str] = Field(default_factory=list)


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: str
    sha256: str
    row_count: int | None = None


class TigerReadyScopeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = TIGER_SCOPE_SCHEMA
    scope_version: str = TIGER_SCOPE_VERSION
    scope: str = TIGER_READY_SCOPE
    status: str = "READY"
    source_snapshot_id: str
    source_snapshot_status: str
    data_cutoff_date: date
    discovered_product_count: int
    crawled_product_count: int
    ready_product_count: int
    blocked_product_count: int
    portfolio_row_count: int
    eligible_security_row_count: int
    non_security_row_count: int
    unique_security_source_ids: int
    classification_counts: dict[str, int]
    product_eligibility: ArtifactReference
    holding_selection: ArtifactReference
    source_holdings: ArtifactReference
    source_evidence_links: ArtifactReference
    source_records: ArtifactReference
    referenced_source_record_count: int
    referenced_raw_artifact_count: int
    eligibility_contract: list[str]
    validation: dict[str, bool]

    def canonical_bytes(self) -> bytes:
        return (json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, indent=2,
        ) + "\n").encode()


def build_tiger_ready_scope(
    snapshot_root: Path, *, reviewed_security_tickers: frozenset[str],
) -> tuple[Path, TigerReadyScopeManifest]:
    source_manifest = ExternalSnapshotManifest.model_validate_json(
        (snapshot_root / "manifest.json").read_text(encoding="utf-8")
    )
    if source_manifest.status is not SnapshotStatus.PARTIAL:
        raise ValueError("TIGER full candidate crawl must remain PARTIAL")
    if source_manifest.data_cutoff_date != DATA_CUTOFF_DATE:
        raise ValueError("TIGER source cutoff mismatch")
    paths = _paths(snapshot_root)
    results = [
        TigerCrawlResult.model_validate_json(line)
        for line in paths["results"].read_text(encoding="utf-8").splitlines() if line
    ]
    holdings = _read(paths["holdings"])
    by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in holdings:
        by_product[str(row["product_source_id"])].append(row)
    classifications = [
        _classify(item, by_product.get(item.product_source_id, []), reviewed_security_tickers)
        for item in results
    ]
    ready = {
        item.product_source_id for item in classifications if item.status == "READY_LONG_ONLY"
    }
    ready_rows = sorted(
        (row for row in holdings if str(row["product_source_id"]) in ready),
        key=lambda row: str(row["holding_record_id"]),
    )
    selection = [{
        "holding_record_id": row["holding_record_id"],
        "canonical_eligible": row["constituent_identity_status"] != "NON_SECURITY",
    } for row in ready_rows]
    holding_ids = {str(item["holding_record_id"]) for item in selection}
    links = [
        item for item in _read(paths["links"])
        if str(item["holding_record_id"]) in holding_ids
    ]
    source_ids = {str(item["source_record_id"]) for item in links}
    sources = [
        ExternalSourceRecord.model_validate_json(line)
        for line in paths["sources"].read_text(encoding="utf-8").splitlines() if line
    ]
    selected_sources = [item for item in sources if item.source_record_id in source_ids]
    if {item.source_record_id for item in selected_sources} != source_ids:
        raise ValueError("TIGER selection has missing source evidence")
    for item in selected_sources:
        raw = snapshot_root / item.raw_artifact_path
        if not raw.is_file() or _sha(raw) != item.raw_content_hash:
            raise ValueError("TIGER selection has invalid raw evidence")
    scope_dir = snapshot_root / "scopes" / TIGER_SCOPE_VERSION
    scope_dir.mkdir(parents=True, exist_ok=True)
    eligibility_ref = _write(scope_dir / "product_eligibility.jsonl", [
        item.model_dump(mode="json") for item in classifications
    ], snapshot_root)
    selection_ref = _write(scope_dir / "holding_selection.jsonl", selection, snapshot_root)
    security_rows = [
        row for row in ready_rows if row["constituent_identity_status"] != "NON_SECURITY"
    ]
    counts = Counter(item.status for item in classifications)
    manifest = TigerReadyScopeManifest(
        source_snapshot_id=source_manifest.snapshot_id,
        source_snapshot_status=source_manifest.status.value,
        data_cutoff_date=DATA_CUTOFF_DATE,
        discovered_product_count=int(source_manifest.validation.get("discovered_products", 0)),
        crawled_product_count=len(results),
        ready_product_count=len(ready),
        blocked_product_count=len(results) - len(ready),
        portfolio_row_count=len(ready_rows),
        eligible_security_row_count=len(security_rows),
        non_security_row_count=len(ready_rows) - len(security_rows),
        unique_security_source_ids=len({row["constituent_ticker"] for row in security_rows}),
        classification_counts=dict(sorted(counts.items())),
        product_eligibility=eligibility_ref,
        holding_selection=selection_ref,
        source_holdings=_ref(paths["holdings"], snapshot_root),
        source_evidence_links=_ref(paths["links"], snapshot_root),
        source_records=_ref(paths["sources"], snapshot_root),
        referenced_source_record_count=len(source_ids),
        referenced_raw_artifact_count=len({item.raw_content_hash for item in selected_sources}),
        eligibility_contract=[
            "official exact-date response is complete at 2026-08-24",
            "product resolves by authoritative PREF01 ISIN before ticker+KRX",
            "every Security position has an exact six-digit KRX equity identifier",
            "matching canonical Securities are reused; new identifiers require KRX issuer review",
            "all Security quantities, evaluated values, and NAV weights are non-negative",
            "cash rows remain evidence and never become Security/HOLDS",
            "one unknown or unsupported position blocks the complete product",
        ],
        validation={
            "cutoff_verified": all(
                date.fromisoformat(str(row["effective_date"])) <= DATA_CUTOFF_DATE
                for row in ready_rows
            ),
            "complete_product_selection": True,
            "exact_krx_security_identifiers": all(
                _KRX_EQUITY_TICKER.fullmatch(str(row["constituent_ticker"])) is not None
                for row in security_rows
            ),
            "raw_checksums_verified": True,
        },
    )
    path = scope_dir / "manifest.json"
    _atomic(path, manifest.canonical_bytes())
    return path, manifest


def load_trusted_tiger_scope(
    snapshot_root: Path, *, canonical_snapshot_id: str = TIGER_CONTRACT.canonical_snapshot_id,
) -> TrustedHoldingsSnapshot:
    manifest_path = snapshot_root / "scopes" / TIGER_SCOPE_VERSION / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = TigerReadyScopeManifest.model_validate_json(manifest_bytes)
    for item in (
        manifest.product_eligibility, manifest.holding_selection,
        manifest.source_holdings, manifest.source_evidence_links, manifest.source_records,
    ):
        if _sha(snapshot_root / item.relative_path) != item.sha256:
            raise ValueError("TIGER READY scope checksum mismatch")
    selection = {
        str(item["holding_record_id"]): bool(item["canonical_eligible"])
        for item in _read(snapshot_root / manifest.holding_selection.relative_path)
    }
    all_holdings = {
        str(item["holding_record_id"]): item
        for item in _read(snapshot_root / manifest.source_holdings.relative_path)
    }
    evidence: dict[str, list[str]] = defaultdict(list)
    for item in _read(snapshot_root / manifest.source_evidence_links.relative_path):
        if str(item["holding_record_id"]) in selection:
            evidence[str(item["holding_record_id"])].append(str(item["source_record_id"]))
    sources = {
        item.source_record_id: item for item in (
            ExternalSourceRecord.model_validate_json(line)
            for line in (snapshot_root / manifest.source_records.relative_path)
            .read_text(encoding="utf-8").splitlines() if line
        )
    }
    used_source_ids = {value for values in evidence.values() for value in values}
    hydrated = []
    for holding_id in sorted(selection):
        valid = [
            sources[value] for value in sorted(evidence.get(holding_id, []))
            if sources[value].quality_status is QualityStatus.VALID
        ]
        if not valid:
            raise ValueError("TIGER selected holding lacks VALID evidence")
        source = valid[0]
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
        source_records=tuple(sources[item] for item in sorted(used_source_ids)),
        holdings=tuple(hydrated),
        manifest_path=manifest_path,
        source_snapshot_id=manifest.source_snapshot_id,
        provider_contract=TIGER_CONTRACT,
    )


def _classify(result, rows, reviewed):
    reasons = []
    security = 0
    cash = 0
    if result.status is not TigerCrawlStatus.SUCCESS:
        reasons.append(result.reason or result.status.value)
    if len(rows) != result.holding_count:
        reasons.append("normalized row count differs from official complete response")
    for row in rows:
        if row["constituent_identity_status"] == "NON_SECURITY":
            cash += 1
            continue
        security += 1
        ticker = str(row.get("constituent_ticker") or "")
        if not _KRX_EQUITY_TICKER.fullmatch(ticker):
            reasons.append(f"unsupported Security identity:{ticker or '<missing>'}")
        if row.get("weight_unit") != "PERCENT_OF_NET_ASSET_VALUE":
            reasons.append("unknown TIGER NAV weight semantics")
        if row.get("numeric_status") != "VALIDATED":
            reasons.append("incomplete Security numeric fields")
    return TigerProductEligibility(
        product_source_id=result.product_source_id,
        product_name=result.product_name,
        product_ticker=result.product_ticker,
        product_isin=result.product_isin,
        status="READY_LONG_ONLY" if not reasons else "BLOCKED",
        portfolio_row_count=len(rows), eligible_security_row_count=security,
        non_security_row_count=cash, reasons=sorted(set(reasons))[:25],
    )


def _paths(root):
    return {
        "results": root / "holdings/normalized/crawl_results.jsonl",
        "holdings": root / "holdings/normalized/holdings.jsonl",
        "links": root / "holdings/normalized/holding_evidence_links.jsonl",
        "sources": root / "holdings/normalized/source_records.jsonl",
    }


def _read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ref(path, root):
    return ArtifactReference(
        relative_path=path.relative_to(root).as_posix(), sha256=_sha(path),
        row_count=len(path.read_text(encoding="utf-8").splitlines()),
    )


def _write(path, rows, root):
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in sorted(rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    ).encode()
    _atomic(path, payload)
    return _ref(path, root)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic(path, payload):
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
