from __future__ import annotations

from datetime import date
from typing import Iterable

from app.external_data.holdings.models import ExternalHolding
from app.external_data.manifest import NormalizedOutputEntry, SnapshotWorkspace
from app.external_data.models import EXTERNAL_HOLDINGS_SCHEMA


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
    rows: Iterable[ExternalHolding], *, source_record_id: str, snapshot_id: str,
) -> list[ExternalHolding]:
    accepted: dict[str, ExternalHolding] = {}
    for row in rows:
        if row.source_record_id != source_record_id:
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
    return workspace.write_normalized_jsonl(
        category="holdings",
        filename="holdings.jsonl",
        schema_version=EXTERNAL_HOLDINGS_SCHEMA,
        canonical_rows=[row.canonical_json() for row in validated],
    )
