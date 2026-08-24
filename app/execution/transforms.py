from app.domain.models import (
    ExecutionContext,
    QueryOperation,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
)


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
            else dependency_records[-1]
        )
        for record in primary_records:
            entity_id = record.entity_id
            if entity_id is None or entity_id not in common_entity_ids:
                continue
            if entity_id in emitted:
                continue
            emitted.add(entity_id)
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
                            "ranking_applied": False,
                            "fusion_provenance": fusion_provenance,
                            "semantic_matches": semantic_matches,
                        },
                    },
                )
            )
        limit = step.inputs.get("limit")
        if limit is None:
            return transformed
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("internal transform limit must be positive")
        return transformed[:limit]
