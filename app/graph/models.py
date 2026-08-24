from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class GraphNode:
    entity_id: str
    node_type: str
    labels: tuple[str, ...]
    properties: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    subject_id: str
    edge_type: str
    object_id: str
    properties: dict[str, Any]


@dataclass(slots=True)
class GraphBuildStats:
    source_rows: dict[str, int] = field(default_factory=dict)
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_relation: dict[str, int] = field(default_factory=dict)
    skipped_null_relations: dict[str, int] = field(default_factory=dict)
    skipped_sentinel_relations: dict[str, int] = field(default_factory=dict)
    unresolved_target_relations: dict[str, int] = field(default_factory=dict)

    @property
    def total_nodes(self) -> int:
        return sum(self.nodes_by_type.values())

    @property
    def total_edges(self) -> int:
        return sum(self.edges_by_relation.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": dict(sorted(self.source_rows.items())),
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "nodes_by_type": dict(sorted(self.nodes_by_type.items())),
            "edges_by_relation": dict(sorted(self.edges_by_relation.items())),
            "skipped_null_relations": dict(
                sorted(self.skipped_null_relations.items())
            ),
            "skipped_sentinel_relations": dict(
                sorted(self.skipped_sentinel_relations.items())
            ),
            "unresolved_target_relations": dict(
                sorted(self.unresolved_target_relations.items())
            ),
        }


@dataclass(frozen=True, slots=True)
class GraphBuildData:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    stats: GraphBuildStats


@dataclass(frozen=True, slots=True)
class GraphMetadata:
    graph_version: str
    dataset_snapshot: str
    built_at: datetime
    status: str
    statistics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompiledGraphQuery:
    cypher: str
    parameters: dict[str, Any]
    relations: tuple[str, ...]
    directions: tuple[str, ...]
