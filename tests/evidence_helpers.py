"""Compact evidence fixtures used by semantic-safety regression tests."""

from __future__ import annotations

import asyncio
from typing import Any

from app.domain.models import (
    Evidence,
    EvidenceBundle,
    ExecutionResult,
    GroundedQuery,
    GroundedSort,
    GroundingStatus,
    ParsedQuery,
    QueryIntent,
    SortSpec,
)
from app.evidence.quality import StaticFieldQualityProvider
from app.evidence.validator import QualityAwareEvidenceValidator


def make_query(*, sort_fields: list[str] | None = None) -> GroundedQuery:
    fields = sort_fields or []
    parsed = ParsedQuery(
        original_question="test question",
        intent=QueryIntent.SEARCH_PRODUCT,
        sort=[SortSpec(field=field, direction="desc") for field in fields],
    )
    return GroundedQuery(
        parsed_query=parsed,
        grounded_sort=[
            GroundedSort(
                raw_sort=item,
                canonical_field=item.field,
                status=GroundingStatus.RESOLVED,
            )
            for item in parsed.sort
        ],
    )


def make_evidence(
    *,
    field: str,
    value: str,
    metadata: dict[str, Any] | None = None,
) -> Evidence:
    return Evidence(
        source_type="rdb",
        source_id=f"test:{field}",
        entity_id="P1",
        field=field,
        value=value,
        dataset_snapshot="2026-07-11",
        metadata=metadata or {"real_rdb": True},
    )


def make_bundle(
    evidence: list[Evidence],
    execution_result: ExecutionResult | None = None,
) -> EvidenceBundle:
    return EvidenceBundle(
        question="test question",
        evidence=evidence,
        execution_result=execution_result,
    )


def validate(query: GroundedQuery, bundle: EvidenceBundle):
    return asyncio.run(
        QualityAwareEvidenceValidator(StaticFieldQualityProvider()).validate(
            query, bundle
        )
    )
