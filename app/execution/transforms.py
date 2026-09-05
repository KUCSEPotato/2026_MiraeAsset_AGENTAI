from app.domain.models import (
    ExecutionContext,
    QueryOperation,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
    StepExecutionStatus,
)
from app.retrieval.exceptions import IncompleteCandidateSetError, RetrieverUnavailableError


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
        dependencies = [context.step_results[dependency_id] for dependency_id in step.depends_on]
        if any(result.status is not StepExecutionStatus.SUCCESS for result in dependencies):
            raise IncompleteCandidateSetError("candidate merge requires successful dependencies")
        dependency_records = [result.records for result in dependencies]
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
        _validate_snapshot_identity(
            dependency_records,
            required=bool(ranking_requested or step.inputs.get("require_complete_candidates")),
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
                or rankable != returned
                or not isinstance(ranked_ids, list)
                or len(ranked_ids) != returned
                or ranked_ids != list(dict.fromkeys(
                    record.entity_id for record in primary.records if record.entity_id is not None
                ))
            ):
                raise IncompleteCandidateSetError(
                    "global ranking requires a complete pre-ranked RDB candidate set"
                )
        if ranking_requested or step.inputs.get("require_complete_candidates"):
            for result in dependencies:
                if not _complete_candidates(result.retrieval_metadata):
                    raise IncompleteCandidateSetError(
                        f"candidate merge requires a complete set from {result.step_id}"
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

        # Select entity IDs before projecting: every RDB fact and graph path
        # remains evidence for each selected entity. Semantic hits annotate that
        # evidence; their similarity score is never treated as a financial value.
        factual_records = [
            record for records in dependency_records for record in records
            if record.source in {RetrievalSource.RDB.value, RetrievalSource.GRAPH.value}
        ] or primary_records
        seen: set[tuple[str, str, str | None]] = set()
        records_to_emit = []
        for entity_id in selected_entity_ids:
            for record in factual_records:
                key = (record.source, record.source_id, record.payload.get("field"))
                if record.entity_id == entity_id and key not in seen:
                    seen.add(key)
                    records_to_emit.append(record)
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
                    "dataset_snapshot": candidate.metadata.get("dataset_snapshot"),
                    "generation": candidate.metadata.get("generation"),
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
                        "metadata": {
                            **record.metadata,
                            "origin_step_id": record.step_id,
                            "origin_source": record.source,
                            "origin_source_id": record.source_id,
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


def _complete_candidates(metadata: dict) -> bool:
    explicit = metadata.get("counts", {}).get("candidate_set_complete")
    if explicit is not None:
        return type(explicit) in {bool, int} and explicit == 1
    total = metadata.get("rankable_total")
    if total is None:
        total = metadata.get("total_matches")
    returned = metadata.get("returned_count")
    return (
        type(total) is int and type(returned) is int
        and 0 <= total <= returned
    )


def _validate_snapshot_identity(
    dependencies: list[list[RetrievalRecord]], *, required: bool = False,
) -> None:
    records = [record for group in dependencies for record in group]
    if len({record.source for record in records}) < 2:
        return
    real_records = any(
        record.metadata.get("real_rdb") or record.metadata.get("real_graph")
        or record.metadata.get("repository_version") == "v2"
        for record in records
    )
    for key in ("dataset_snapshot", "generation", "snapshot_identity"):
        values = {record.metadata[key] for record in records if record.metadata.get(key) is not None}
        if len(values) > 1:
            raise RetrieverUnavailableError(f"federated evidence {key} mismatch")
        if key == "dataset_snapshot" and (required or real_records) and any(
            not record.metadata.get(key) for record in records
        ):
            raise RetrieverUnavailableError("federated evidence snapshot identity is missing")
        if key == "generation" and values and any(
            not record.metadata.get(key) for record in records
        ):
            raise RetrieverUnavailableError("federated evidence generation is missing")
