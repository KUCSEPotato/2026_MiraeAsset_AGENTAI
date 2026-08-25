from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any


ALLOWED_GRAPH_RELATIONS = frozenset({
    "managedBy", "issuedBy", "tracks", "referencesBenchmark"
})


@dataclass(frozen=True, slots=True)
class GraphProjectionEdge:
    canonical_product_id: str
    relation_type: str
    target_id: str
    target_type: str
    target_label: str
    source_record_id: str
    source_column: str
    snapshot_date: str


def build_graph_edges(rows: Iterable[Mapping[str, Any]]) -> list[GraphProjectionEdge]:
    """Build Neo4j-independent upsert records; unapproved relations stay in RDB only."""
    return [GraphProjectionEdge(
        canonical_product_id=str(row["canonical_product_id"]),
        relation_type=str(row["relation_type"]), target_id=str(row["target_id"]),
        target_type=str(row["target_type"]), target_label=str(row["target_label"]),
        source_record_id=str(row["source_record_id"]),
        source_column=str(row["source_column"]), snapshot_date=str(row["dataset_snapshot"]),
    ) for row in rows if row["relation_type"] in ALLOWED_GRAPH_RELATIONS]
