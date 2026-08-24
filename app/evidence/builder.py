import json
from typing import Any

from app.domain.models import (
    Evidence,
    EvidenceBundle,
    ExecutionResult,
    GroundedQuery,
    RetrievalRecord,
)


def _to_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


class GenericEvidenceBuilder:
    async def build(
        self,
        query: GroundedQuery,
        records: list[RetrievalRecord],
        execution_result: ExecutionResult | None = None,
    ) -> EvidenceBundle:
        normalized = [
            Evidence(
                step_id=record.step_id,
                source_type=record.source,
                source_id=record.source_id,
                entity_id=record.entity_id,
                field=_to_string(record.payload.get("field")),
                value=_to_string(record.payload.get("value")),
                text=_to_string(record.payload.get("text")),
                dataset_snapshot=_to_string(
                    record.metadata.get("dataset_snapshot")
                ),
                observed_at=_to_string(record.metadata.get("observed_at")),
                metadata=record.metadata.copy(),
            )
            for record in records
        ]
        return EvidenceBundle(
            question=query.parsed_query.original_question,
            resolved_entities=query.resolved_entities,
            evidence=normalized,
            missing_fields=(
                query.parsed_query.requested_fields.copy() if not normalized else []
            ),
            execution_result=execution_result,
        )
