from datetime import date
from typing import Iterable

from app.external_data.holdings.models import ExternalHolding


DATA_CUTOFF_DATE = date(2026, 8, 24)


class HoldingsContractError(ValueError):
    pass


class PostCutoffHoldingError(HoldingsContractError):
    pass


def validate_holdings(rows: Iterable[ExternalHolding], *, snapshot_id: str) -> list[ExternalHolding]:
    accepted: dict[str, ExternalHolding] = {}
    for row in rows:
        if row.snapshot_id != snapshot_id:
            raise HoldingsContractError("holding snapshot_id does not match manifest")
        if row.effective_date > DATA_CUTOFF_DATE:
            raise PostCutoffHoldingError("holding effective date is after 2026-08-24")
        previous = accepted.get(row.holding_record_id)
        if previous is not None and previous != row:
            raise HoldingsContractError("holding ID collision has different content")
        accepted[row.holding_record_id] = row
    return sorted(accepted.values(), key=lambda row: row.holding_record_id)
