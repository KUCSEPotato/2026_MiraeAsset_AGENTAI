from __future__ import annotations

from dataclasses import dataclass

from app.external_data.models import QualityStatus


@dataclass(frozen=True, slots=True)
class SnapshotReadinessPolicy:
    """Explicit Crawl-1 rule; source-specific policies may be stricter later."""

    allow_partial: bool = False

    def ready_status(
        self, *, successful_sources: int, failed_sources: int,
        validation_failures: int,
    ) -> str:
        if successful_sources == 0:
            return "FAILED"
        if validation_failures or failed_sources:
            return "PARTIAL" if self.allow_partial else "FAILED"
        return "READY"


FAILED_QUALITY_STATUSES = frozenset({
    QualityStatus.BLOCKED,
    QualityStatus.FETCH_FAILED,
    QualityStatus.PARSE_FAILED,
    QualityStatus.VALIDATION_FAILED,
})
