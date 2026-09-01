from app.domain.models import (
    ExecutionContext,
    QueryOperation,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
)
from app.retrieval.exceptions import IncompleteCandidateSetError


class InternalTransformExecutor:
    """Combine dependency records by entity ID without financial ranking logic."""

    async def execute(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        if step.operation not in {
            QueryOperation.FILTER_CANDIDATES,
            QueryOperation.RANK_CANDIDATES,
        }:
            raise ValueError(f"unsupported internal operation: {step.operation}")
        dependency_records = [
            context.step_results[dependency_id].records
            for dependency_id in step.depends_on
        ]
        if not dependency_records:
            return []
        ranking_requested = (
            step.operation is QueryOperation.RANK_CANDIDATES
            and bool(
                step.inputs.get("sort")
                or step.inputs.get("sort_operations")
                or step.inputs.get("comparison_contracts")
            )
        )
        if ranking_requested:
            primary = context.step_results[step.depends_on[0]]
            metadata = primary.retrieval_metadata
            rankable = metadata.get("rankable_total")
            returned = metadata.get("returned_count")
            ranked_ids = metadata.get("ranked_candidate_ids")
            if (
                not isinstance(rankable, int)
                or not isinstance(returned, int)
                or rankable > returned
                or not isinstance(ranked_ids, list)
                or len(ranked_ids) != returned
            ):
                raise IncompleteCandidateSetError(
                    "global ranking requires a complete pre-ranked RDB candidate set"
                )

        entity_sets = [
            {record.entity_id for record in records if record.entity_id is not None}
            for records in dependency_records
        ]
        common_entity_ids = set.intersection(*entity_sets) if entity_sets else set()
        transformed: list[RetrievalRecord] = []
        emitted: set[str] = set()
        primary_records = (
            dependency_records[0]
            if step.operation is QueryOperation.RANK_CANDIDATES
            else _primary_evidence_records(dependency_records)
        )
        selected_entity_ids: list[str] = []
        for record in primary_records:
            entity_id = record.entity_id
            if entity_id is None or entity_id not in common_entity_ids:
                continue
            if entity_id in emitted:
                continue
            emitted.add(entity_id)
            selected_entity_ids.append(entity_id)
        limit = step.inputs.get("limit")
        if limit is not None:
            if not isinstance(limit, int) or limit <= 0:
                raise ValueError("internal transform limit must be positive")
            selected_entity_ids = selected_entity_ids[:limit]

        # A ranked RDB result can contain several projected fact records for one
        # entity (for example name plus the ranking metric).  Select the Top-N
        # entity IDs first, then retain every projected record for those IDs so
        # the intersection cannot discard the metric evidence used to rank it.
        selected = set(selected_entity_ids)
        records_to_emit = (
            [record for record in primary_records if record.entity_id in selected]
            if step.operation is QueryOperation.RANK_CANDIDATES
            else [
                next(
                    record
                    for record in primary_records
                    if record.entity_id == entity_id
                )
                for entity_id in selected_entity_ids
            ]
        )
        for record in records_to_emit:
            entity_id = record.entity_id
            if entity_id is None:
                continue
            matching_records = [
                candidate
                for records in dependency_records
                for candidate in records
                if candidate.entity_id == entity_id
            ]
            fusion_provenance = [
                {
                    "step_id": candidate.step_id,
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "retrieval_score": candidate.metadata.get("retrieval_score"),
                    "retrieval_score_type": candidate.metadata.get(
                        "retrieval_score_type"
                    ),
                }
                for candidate in matching_records
            ]
            semantic_matches = [
                {
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "field": candidate.payload.get("field"),
                    "text": candidate.payload.get("text"),
                    "retrieval_score": candidate.metadata.get("retrieval_score"),
                    "retrieval_score_type": candidate.metadata.get(
                        "retrieval_score_type"
                    ),
                }
                for candidate in matching_records
                if candidate.source
                in {RetrievalSource.VECTOR.value, RetrievalSource.BM25.value}
            ]
            transformed.append(
                record.model_copy(
                    deep=True,
                    update={
                        "step_id": step.step_id,
                        "source": RetrievalSource.INTERNAL.value,
                        "source_id": f"{step.step_id}:{entity_id}",
                        "metadata": {
                            **record.metadata,
                            "origin_step_id": record.step_id,
                            "dependency_step_ids": step.depends_on,
                            "transform_operation": step.operation.value,
                            "ranking_applied": ranking_requested,
                            "fusion_provenance": fusion_provenance,
                            "semantic_matches": semantic_matches,
                        },
                    },
                )
            )
        return transformed


def _primary_evidence_records(
    dependencies: list[list[RetrievalRecord]],
) -> list[RetrievalRecord]:
    """Preserve factual/relation evidence while semantic hits remain metadata."""
    for preferred in (RetrievalSource.GRAPH.value, RetrievalSource.RDB.value):
        for records in dependencies:
            if any(record.source == preferred for record in records):
                return records
    return dependencies[-1]
