from __future__ import annotations

from typing import Any

from app.domain.models import QueryOperation, QueryStep, RetrievalSource
from app.graph.mapping import GraphMappingRegistry
from app.graph.models import CompiledGraphQuery
from app.retrieval.exceptions import GraphQueryCompilationError


class GraphQueryCompiler:
    """Compile structured, allow-listed paths into parameterized Cypher."""

    def __init__(
        self,
        registry: GraphMappingRegistry,
        *,
        snapshot: str,
        max_depth: int = 2,
        limit: int = 100,
    ) -> None:
        if not 1 <= max_depth <= 2:
            raise ValueError("graph traversal depth must be 1 or 2")
        self._registry = registry
        self._snapshot = snapshot
        self._max_depth = max_depth
        self._limit = limit

    def compile(
        self,
        step: QueryStep,
        path: dict[str, Any],
        *,
        candidate_ids: list[str] | None = None,
    ) -> CompiledGraphQuery:
        if step.source is not RetrievalSource.GRAPH:
            raise GraphQueryCompilationError(
                "Graph compiler requires a graph step"
            )
        if step.operation is not QueryOperation.RELATIONSHIP_SEARCH:
            raise GraphQueryCompilationError(
                f"unsupported graph operation: {step.operation.value}"
            )
        unsupported_keys = set(path) - {
            "relations",
            "directions",
            "raw_relations",
            "target_values",
            "target_types",
            "constraint_ids",
        }
        if unsupported_keys:
            raise GraphQueryCompilationError(
                "unsupported graph path inputs: "
                + ",".join(sorted(unsupported_keys))
            )
        relations = path.get("relations")
        directions = path.get("directions")
        if not isinstance(relations, list) or not relations:
            raise GraphQueryCompilationError("graph path requires relations")
        if len(relations) > self._max_depth:
            raise GraphQueryCompilationError("graph path exceeds maximum depth")
        if not isinstance(directions, list) or len(directions) != len(relations):
            raise GraphQueryCompilationError(
                "every graph relation requires one direction"
            )
        if not all(direction in {"outgoing", "incoming"} for direction in directions):
            raise GraphQueryCompilationError("unsupported graph direction")
        target_values = path.get("target_values", [None] * len(relations))
        target_types = path.get("target_types", [None] * len(relations))
        if (
            not isinstance(target_values, list)
            or len(target_values) != len(relations)
            or not isinstance(target_types, list)
            or len(target_types) != len(relations)
        ):
            raise GraphQueryCompilationError(
                "relation targets must align with graph path depth"
            )

        mappings = []
        try:
            mappings = [self._registry.get(str(item)) for item in relations]
        except ValueError as exc:
            raise GraphQueryCompilationError(str(exc)) from exc

        pattern = "(n0:M10Entity)"
        for index, (mapping, direction) in enumerate(
            zip(mappings, directions, strict=True)
        ):
            next_node = f"(n{index + 1}:M10Entity)"
            relation = f"[r{index}:{mapping.edge_type}]"
            pattern += (
                f"-{relation}->{next_node}"
                if direction == "outgoing"
                else f"<-{relation}-{next_node}"
            )

        node_snapshot_checks = " AND ".join(
            f"n{index}.dataset_snapshot = $snapshot"
            for index in range(len(relations) + 1)
        )
        edge_snapshot_checks = " AND ".join(
            f"r{index}.dataset_snapshot = $snapshot"
            for index in range(len(relations))
        )
        conditions = [node_snapshot_checks, edge_snapshot_checks]
        target_parameters: dict[str, Any] = {}
        for index, (direction, target_value, target_type) in enumerate(
            zip(directions, target_values, target_types, strict=True)
        ):
            if target_value is None:
                continue
            target_node = f"n{index + 1}" if direction == "outgoing" else f"n{index}"
            value_parameter = f"target_value_{index}"
            conditions.append(
                f"coalesce({target_node}.canonical_value, "
                f"{target_node}.display_name, {target_node}.identifier_value) "
                f"= ${value_parameter}"
            )
            target_parameters[value_parameter] = str(target_value)
            if target_type is not None:
                type_parameter = f"target_type_{index}"
                conditions.append(f"{target_node}.node_type = ${type_parameter}")
                target_parameters[type_parameter] = str(target_type)
        source_node_ids = step.inputs.get("source_node_ids", [])
        if not isinstance(source_node_ids, list):
            raise GraphQueryCompilationError("source_node_ids must be a list")
        if source_node_ids:
            conditions.append("n0.entity_id IN $source_node_ids")
        candidates = candidate_ids or []
        if candidates:
            node_names = ", ".join(
                f"n{index}" for index in range(len(relations) + 1)
            )
            conditions.append(
                f"any(candidate IN [{node_names}] "
                "WHERE candidate.entity_id IN $candidate_ids)"
            )
        if not source_node_ids and not candidates and not target_parameters:
            raise GraphQueryCompilationError(
                "graph traversal requires resolved source nodes or RDB candidates"
            )

        node_projection = ", ".join(
            f"properties(n{index})" for index in range(len(relations) + 1)
        )
        edge_projection = ", ".join(
            f"properties(r{index})" for index in range(len(relations))
        )
        cypher = (
            f"MATCH path={pattern} WHERE {' AND '.join(conditions)} "
            f"RETURN [{node_projection}] AS nodes, "
            f"[{edge_projection}] AS edges "
            "ORDER BY n0.entity_id LIMIT $limit"
        )
        limit = step.inputs.get("limit", self._limit)
        if not isinstance(limit, int) or limit <= 0:
            raise GraphQueryCompilationError("graph limit must be positive")
        return CompiledGraphQuery(
            cypher=cypher,
            parameters={
                "snapshot": self._snapshot,
                "source_node_ids": source_node_ids,
                "candidate_ids": candidates,
                "limit": min(limit, self._limit),
                **target_parameters,
            },
            relations=tuple(mapping.canonical_relation for mapping in mappings),
            directions=tuple(str(item) for item in directions),
        )


def graph_paths(step: QueryStep) -> list[dict[str, Any]]:
    paths = step.inputs.get("paths")
    if not isinstance(paths, list) or not paths:
        raise GraphQueryCompilationError("graph step requires structured paths")
    if not all(isinstance(path, dict) for path in paths):
        raise GraphQueryCompilationError("graph paths must be objects")
    return paths
