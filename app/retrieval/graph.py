from __future__ import annotations

from typing import Any

from app.domain.models import (
    ExecutionContext,
    QueryStep,
    RetrievalRecord,
    StepExecutionStatus,
)
from app.graph.backend import Neo4jGraphBackend
from app.graph.compiler import GraphQueryCompiler, graph_paths
from app.retrieval.exceptions import RetrieverUnavailableError


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
        candidate_ids = _dependency_candidates(step, context)
        try:
            metadata = await self._backend.assert_ready(
                expected_snapshot=self._snapshot
            )
            results: list[RetrievalRecord] = []
            emitted: set[str] = set()
            for path in graph_paths(step):
                compiled = self._compiler.compile(
                    step,
                    path,
                    candidate_ids=candidate_ids,
                )
                rows = await self._backend.query(
                    compiled.cypher, compiled.parameters
                )
                for row in rows:
                    record = _record_from_path(
                        step,
                        row,
                        compiled.relations,
                        compiled.directions,
                        self._snapshot,
                        metadata.graph_version,
                        candidate_ids,
                    )
                    if record.source_id in emitted:
                        continue
                    emitted.add(record.source_id)
                    results.append(record)
            return results
        except RetrieverUnavailableError:
            raise
        except Exception as exc:
            raise RetrieverUnavailableError(
                f"Graph retrieval failed: {exc}"
            ) from exc


def _dependency_candidates(
    step: QueryStep,
    context: ExecutionContext,
) -> list[str]:
    dependency_ids = step.inputs.get("candidate_ids_from", step.depends_on)
    if not dependency_ids:
        return []
    if not isinstance(dependency_ids, list):
        raise RetrieverUnavailableError("candidate_ids_from must be a list")
    candidates: set[str] = set()
    for dependency_id in dependency_ids:
        result = context.step_results.get(dependency_id)
        if result is None or result.status is not StepExecutionStatus.SUCCESS:
            continue
        candidates.update(
            record.entity_id
            for record in result.records
            if record.entity_id is not None
        )
    return sorted(candidates)


def _record_from_path(
    step: QueryStep,
    row: dict[str, Any],
    relations: tuple[str, ...],
    directions: tuple[str, ...],
    snapshot: str,
    graph_version: str,
    candidate_ids: list[str],
) -> RetrievalRecord:
    nodes = [dict(item) for item in row.get("nodes", [])]
    edges = [dict(item) for item in row.get("edges", [])]
    if not nodes or not edges:
        raise ValueError("Neo4j returned an empty graph path")
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
            "edge_type": edge.get("edge_type"),
            "source_dataset": edge.get("source_dataset"),
            "source_record_keys": edge.get("source_record_keys", []),
            "source_fields": edge.get("source_fields", []),
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
