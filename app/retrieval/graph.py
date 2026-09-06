from __future__ import annotations

from typing import Any

from app.domain.models import (
    ExecutionContext,
    QueryStep,
    RetrievalRecord,
    RetrievalResult,
    StepExecutionStatus,
)
from app.graph.backend import Neo4jGraphBackend
from app.graph.compiler import GraphQueryCompiler, graph_paths
from app.retrieval.exceptions import (
    IncompleteCandidateSetError,
    RetrievalError,
    RetrieverUnavailableError,
)


class RealGraphRetriever:
    def __init__(
        self,
        backend: Neo4jGraphBackend,
        compiler: GraphQueryCompiler,
        *,
        snapshot: str,
    ) -> None:
        self._backend = backend
        self._compiler = compiler
        self._snapshot = snapshot

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        return (await self.retrieve_with_result(step, context)).records

    async def retrieve_with_result(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> RetrievalResult:
        candidate_ids = _dependency_candidates(step, context)
        try:
            metadata = await self._backend.assert_ready(
                expected_snapshot=self._snapshot
            )
            generation = getattr(metadata, "generation", None)
            _validate_dependency_identity(step, context, self._snapshot, generation)
            results: list[RetrievalRecord] = []
            emitted: set[str] = set()
            path_totals: list[int] = []
            records_by_path: list[list[RetrievalRecord]] = []
            candidate_set_complete = True
            operator = step.inputs.get("path_operator", "union")
            if operator not in {"and", "union"}:
                raise RetrieverUnavailableError("unsupported graph path operator")
            for path in graph_paths(step):
                compiled = self._compiler.compile(
                    step,
                    path,
                    candidate_ids=candidate_ids,
                )
                if candidate_ids == []:
                    path_totals.append(0)
                    records_by_path.append([])
                    continue
                count_rows = await self._backend.query(
                    compiled.count_cypher, compiled.parameters
                )
                count = count_rows[0]["total_matches"]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise RetrieverUnavailableError("invalid graph path count")
                path_totals.append(count)
                rows = await self._backend.query(
                    compiled.cypher, compiled.parameters
                )
                candidate_set_complete &= len(rows) == count
                path_records: list[RetrievalRecord] = []
                for row in rows:
                    record = _record_from_path(
                        step,
                        row,
                        compiled.relations,
                        compiled.directions,
                        self._snapshot,
                        getattr(
                            metadata,
                            "graph_version",
                            getattr(metadata, "projection_version", "unknown"),
                        ),
                        candidate_ids or [],
                        generation,
                    )
                    path_records.append(record)
                    if record.source_id in emitted:
                        continue
                    emitted.add(record.source_id)
                    results.append(record)
                records_by_path.append(path_records)
            if step.inputs.get("require_complete_candidates") and not candidate_set_complete:
                raise IncompleteCandidateSetError(
                    "graph composition requires all matching paths before projection or ranking"
                )
            if operator == "and" and records_by_path:
                candidates = set.intersection(*(
                    {record.entity_id for record in records}
                    for records in records_by_path
                ))
                results = [record for record in results if record.entity_id in candidates]
            result_limit = step.inputs.get("result_limit")
            if result_limit is not None:
                if type(result_limit) is not int or result_limit <= 0:
                    raise RetrieverUnavailableError("graph result_limit must be positive")
                entity_ids = list(dict.fromkeys(record.entity_id for record in results))
                candidate_set_complete &= len(entity_ids) <= result_limit
                selected = set(entity_ids[:result_limit])
                results = [record for record in results if record.entity_id in selected]
            # A single path has an exact count.  Multi-path plans may overlap;
            # retain an explicit per-path count instead of inventing a union.
            total_matches = path_totals[0] if len(path_totals) == 1 else None
            return RetrievalResult(
                records=results,
                total_matches=total_matches,
                returned_count=len(results),
                window_limit=compiled.parameters["limit"] if path_totals else None,
                counts={
                    "candidate_set_complete": int(candidate_set_complete),
                    "candidate_count": len({record.entity_id for record in results}),
                    "path_count": len(path_totals),
                    "path_total_sum": sum(path_totals),
                    **({"path_total_matches": total_matches} if total_matches is not None else {}),
                },
            )
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrieverUnavailableError(
                f"Graph retrieval failed: {exc}"
            ) from exc


def _dependency_candidates(
    step: QueryStep,
    context: ExecutionContext,
) -> list[str] | None:
    dependency_ids = step.inputs.get("candidate_ids_from", step.depends_on)
    if not isinstance(dependency_ids, list) or not all(
        isinstance(item, str) and item in step.depends_on for item in dependency_ids
    ):
        raise RetrieverUnavailableError("candidate_ids_from must be a list")
    if not dependency_ids:
        return None
    candidate_sets: list[set[str]] = []
    for dependency_id in dependency_ids:
        result = context.step_results.get(dependency_id)
        if result is None or result.status is not StepExecutionStatus.SUCCESS:
            raise RetrieverUnavailableError("graph candidate dependency is incomplete")
        candidate_sets.append({
            record.entity_id
            for record in result.records
            if record.entity_id is not None
        })
    return sorted(set.intersection(*candidate_sets))


def _validate_dependency_identity(step, context, snapshot, generation) -> None:
    for dependency in step.inputs.get("candidate_ids_from", step.depends_on):
        for record in context.step_results[dependency].records:
            actual_snapshot = record.metadata.get("dataset_snapshot")
            actual_generation = record.metadata.get("generation")
            if actual_snapshot is not None and actual_snapshot != snapshot:
                raise RetrieverUnavailableError("graph candidate snapshot mismatch")
            if generation is not None and actual_generation is not None and actual_generation != generation:
                raise RetrieverUnavailableError("graph candidate generation mismatch")


def _record_from_path(
    step: QueryStep,
    row: dict[str, Any],
    relations: tuple[str, ...],
    directions: tuple[str, ...],
    snapshot: str,
    graph_version: str,
    candidate_ids: list[str],
    generation: str | None = None,
) -> RetrievalRecord:
    nodes = [dict(item) for item in row.get("nodes", [])]
    edges = [dict(item) for item in row.get("edges", [])]
    if not nodes or not edges:
        raise ValueError("Neo4j returned an empty graph path")
    if len(edges) != len(relations) or len(nodes) != len(edges) + 1:
        raise ValueError("Neo4j path does not match the compiled traversal depth")
    entity_id = _result_entity_id(nodes, directions, candidate_ids)
    labels = [str(node.get("display_name") or node.get("entity_id")) for node in nodes]
    path_text = " -> ".join(labels)
    edge_ids = [str(edge["edge_id"]) for edge in edges]
    relation_key = "/".join(relations)
    source_id = "graph:" + ":".join(edge_ids)
    target = labels[-1]
    provenance = [
        {
            "edge_id": edge.get("edge_id"),
            "canonical_fact_id": edge.get("canonical_fact_id"),
            "evidence_assertion_ids": edge.get("evidence_assertion_ids", []),
            "edge_type": edge.get("edge_type"),
            "source_dataset": edge.get("source_dataset"),
            "source_record_keys": edge.get("source_record_keys", []),
            "source_fields": edge.get("source_fields", []),
            **{
                key: edge[key]
                for key in (
                    "effective_date", "external_holding_record_id", "source_provider",
                    "weight_normalized", "weight_unit", "weight_scale",
                )
                if key in edge
            },
        }
        for edge in edges
    ]
    return RetrievalRecord(
        step_id=step.step_id,
        source="graph",
        source_id=source_id,
        entity_id=entity_id,
        payload={
            "field": f"graph.{relation_key}",
            "value": target,
            "text": path_text,
        },
        metadata={
            "dataset_snapshot": snapshot,
            **({"generation": generation, "snapshot_identity": f"{generation}:{snapshot}"}
               if generation is not None else {}),
            "graph_version": graph_version,
            "relations": list(relations),
            "directions": list(directions),
            "path_node_ids": [node.get("entity_id") for node in nodes],
            "path_provenance": provenance,
            "multi_valued_relation": True,
            "real_graph": True,
        },
    )


def _result_entity_id(
    nodes: list[dict[str, Any]],
    directions: tuple[str, ...],
    candidate_ids: list[str],
) -> str:
    candidate_set = set(candidate_ids)
    for node in nodes:
        if node.get("entity_id") in candidate_set:
            return str(node["entity_id"])
    preferred = nodes[0] if directions[0] == "outgoing" else nodes[-1]
    return str(preferred["entity_id"])
