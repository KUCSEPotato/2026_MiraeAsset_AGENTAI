"""Explicit coverage contracts for reviewed provider Holdings scopes.

Coverage is a property of a named universe, not of the ``HOLDS`` predicate in
the abstract.  Keeping the registry small and typed prevents a KODEX subset
from being advertised as complete DomesticETF or ETF coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


KODEX_READY_SCOPE = "KODEX_LONG_ONLY_COMPATIBLE"
TIGER_READY_SCOPE = "TIGER_LONG_ONLY_COMPATIBLE"
ISHARES_READY_SCOPE = "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS"
COMBINED_READY_SCOPES = frozenset({
    KODEX_READY_SCOPE, TIGER_READY_SCOPE, ISHARES_READY_SCOPE,
})


class HoldingsCoverageStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    NOT_READY = "NOT_READY"
    UNSUPPORTED_POSITION_SEMANTICS = "UNSUPPORTED_POSITION_SEMANTICS"


@dataclass(frozen=True, slots=True)
class HoldingsCoverage:
    scope: str
    status: HoldingsCoverageStatus
    reason: str


class HoldingsCoverageRegistry:
    """Authoritative runtime coverage declarations for the current snapshot."""

    _entries = {
        KODEX_READY_SCOPE: HoldingsCoverage(
            KODEX_READY_SCOPE,
            HoldingsCoverageStatus.READY,
            "product-level complete long-only portfolios with current C2 Security identity",
        ),
        "KODEX_FULL": HoldingsCoverage(
            "KODEX_FULL",
            HoldingsCoverageStatus.PARTIAL,
            "unsupported position semantics, identity gaps, and one incomplete response remain",
        ),
        "KODEX_DERIVATIVE_OR_LEVERAGED": HoldingsCoverage(
            "KODEX_DERIVATIVE_OR_LEVERAGED",
            HoldingsCoverageStatus.UNSUPPORTED_POSITION_SEMANTICS,
            "Short/derivative/leveraged positions are outside the canonical HOLDS model",
        ),
        TIGER_READY_SCOPE: HoldingsCoverage(
            TIGER_READY_SCOPE,
            HoldingsCoverageStatus.READY,
            "complete long-only TIGER portfolios whose positions reuse reviewed Securities",
        ),
        ISHARES_READY_SCOPE: HoldingsCoverage(
            ISHARES_READY_SCOPE,
            HoldingsCoverageStatus.READY,
            "complete reviewed equity-Security positions for the selected historical iShares US products",
        ),
        "ISHARES_US_FULL": HoldingsCoverage(
            "ISHARES_US_FULL",
            HoldingsCoverageStatus.PARTIAL,
            "only a reviewed provider subset has product-complete historical Security coverage",
        ),
        "TIGER_FULL": HoldingsCoverage(
            "TIGER_FULL",
            HoldingsCoverageStatus.PARTIAL,
            "the source contains products outside the reviewed long-only Security contract",
        ),
        "DomesticETF": HoldingsCoverage(
            "DomesticETF",
            HoldingsCoverageStatus.PARTIAL,
            "the READY KODEX and TIGER subsets do not cover the complete domestic ETF universe",
        ),
        "ForeignETF": HoldingsCoverage(
            "ForeignETF",
            HoldingsCoverageStatus.PARTIAL,
            "the iShares READY subset does not cover the complete foreign ETF universe",
        ),
        "PublicFund": HoldingsCoverage(
            "PublicFund",
            HoldingsCoverageStatus.NOT_READY,
            "no trusted holdings source is activated",
        ),
        "ETF": HoldingsCoverage(
            "ETF",
            HoldingsCoverageStatus.PARTIAL,
            "the READY provider subsets do not cover the complete ETF universe",
        ),
    }

    def get(self, scope: str) -> HoldingsCoverage:
        return self._entries.get(
            scope,
            HoldingsCoverage(
                scope,
                HoldingsCoverageStatus.NOT_READY,
                "requested holdings universe has no reviewed coverage contract",
            ),
        )

    def is_ready_exact_scope(self, operands: list[str] | None) -> bool:
        if not operands or len(operands) != len(set(operands)):
            return False
        requested = frozenset(operands)
        return requested.issubset(COMBINED_READY_SCOPES)

    @property
    def entries(self) -> tuple[HoldingsCoverage, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))
