from __future__ import annotations

from datetime import date
from typing import Iterable

from app.external_data.holdings.models import ExternalHolding, ExternalHoldingEvidenceLink
from app.external_data.manifest import NormalizedOutputEntry, SnapshotWorkspace
from app.external_data.models import EXTERNAL_HOLDING_EVIDENCE_SCHEMA, EXTERNAL_HOLDINGS_SCHEMA


DATA_CUTOFF_DATE = date(2026, 8, 24)


class HoldingsContractError(ValueError):
    pass


class PostCutoffHoldingError(HoldingsContractError):
    pass


def require_cutoff_eligible(effective_date: date) -> None:
    if effective_date > DATA_CUTOFF_DATE:
        raise PostCutoffHoldingError(
            f"holding effective date {effective_date.isoformat()} is after "
            f"data cutoff {DATA_CUTOFF_DATE.isoformat()}"
        )


def validate_holdings(
    rows: Iterable[ExternalHolding], *, snapshot_id: str,
    source_record_id: str | None = None,
) -> list[ExternalHolding]:
    """Validate both single-source crawler output and a complete C2 snapshot."""

    accepted: dict[str, ExternalHolding] = {}
    for row in rows:
        if source_record_id is not None and row.source_record_id != source_record_id:
            raise HoldingsContractError("holding source_record_id does not match raw source")
        if row.snapshot_id != snapshot_id:
            raise HoldingsContractError("holding snapshot_id does not match workspace")
        require_cutoff_eligible(row.effective_date)
        existing = accepted.get(row.holding_record_id)
        if existing is not None and existing != row:
            raise HoldingsContractError("holding ID collision has different row content")
        accepted[row.holding_record_id] = row
    if not accepted:
        raise HoldingsContractError("holdings output cannot be empty")
    return sorted(accepted.values(), key=lambda item: item.holding_record_id)


def write_holdings(
    workspace: SnapshotWorkspace, rows: Iterable[ExternalHolding],
    *, source_record_id: str,
) -> NormalizedOutputEntry:
    validated = validate_holdings(
        rows, source_record_id=source_record_id, snapshot_id=workspace.snapshot_id,
    )
    semantic_rows = _merge_semantic_rows(workspace, validated)
    output = workspace.write_normalized_jsonl(
        category="holdings",
        filename="holdings.jsonl",
        schema_version=EXTERNAL_HOLDINGS_SCHEMA,
        canonical_rows=semantic_rows.values(),
    )
    evidence_rows = _merge_evidence_links(
        workspace,
        [
            ExternalHoldingEvidenceLink(
                holding_record_id=row.holding_record_id,
                source_record_id=source_record_id,
            )
            for row in validated
        ],
    )
    workspace.write_normalized_jsonl(
        category="holdings",
        filename="holding_evidence_links.jsonl",
        schema_version=EXTERNAL_HOLDING_EVIDENCE_SCHEMA,
        canonical_rows=evidence_rows,
    )
    return output


def _merge_semantic_rows(
    workspace: SnapshotWorkspace, rows: Iterable[ExternalHolding],
) -> dict[str, str]:
    existing = _read_jsonl_by_key(
        workspace.normalized_directory("holdings") / "holdings.jsonl",
        "holding_record_id",
    )
    for row in rows:
        canonical = row.semantic_canonical_json()
        previous = existing.get(row.holding_record_id)
        if previous is not None and previous != canonical:
            raise HoldingsContractError(
                "holding semantic ID collision has different historical fact content"
            )
        existing[row.holding_record_id] = canonical
    return existing


def _merge_evidence_links(
    workspace: SnapshotWorkspace, links: Iterable[ExternalHoldingEvidenceLink],
) -> list[str]:
    path = workspace.normalized_directory("holdings") / "holding_evidence_links.jsonl"
    rows = set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else set()
    rows.update(link.canonical_json() for link in links)
    return sorted(rows)


def _read_jsonl_by_key(path, key: str) -> dict[str, str]:
    if not path.is_file():
        return {}
    import json

    accepted: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        value = json.loads(line)
        identity = str(value[key])
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        previous = accepted.get(identity)
        if previous is not None and previous != canonical:
            raise HoldingsContractError(f"duplicate {key} has conflicting normalized rows")
        accepted[identity] = canonical
    return accepted
