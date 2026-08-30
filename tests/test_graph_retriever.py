import asyncio
from datetime import UTC, datetime

import pytest

from app.domain.models import (
    ExecutionContext,
    QueryOperation,
    QueryPlan,
    QueryStep,
    RetrievalSource,
)
from app.graph.compiler import GraphQueryCompiler
from app.graph.mapping import GraphMappingRegistry
from app.graph.models import GraphMetadata
from app.retrieval.exceptions import RetrieverUnavailableError
from app.retrieval.graph import RealGraphRetriever


class StubGraphBackend:
    def __init__(self, *, stale: bool = False, empty: bool = False) -> None:
        self.stale = stale
        self.empty = empty
        self.queries: list[tuple[str, dict]] = []

    async def assert_ready(self, *, expected_snapshot: str) -> GraphMetadata:
        if self.stale:
            raise RuntimeError("graph snapshot mismatch")
        return GraphMetadata(
            graph_version="m10-minimal-graph-v1",
            dataset_snapshot=expected_snapshot,
            built_at=datetime.now(UTC),
            status="ready",
            statistics={},
        )

    async def query(self, cypher: str, parameters: dict):
        self.queries.append((cypher, parameters))
        if "total_matches" in cypher:
            return [{"total_matches": 0 if self.empty else 1}]
        if self.empty:
            return []
        return [
            {
                "nodes": [
                    {
                        "entity_id": "etf_kr:KR7069500007",
                        "display_name": "KODEX 200",
                    },
                    {
                        "entity_id": "asset_manager:domestic_etf:samsung",
                        "display_name": "삼성",
                    },
                ],
                "edges": [
                    {
                        "edge_id": "edge-1",
                        "edge_type": "MANAGED_BY",
                        "source_dataset": "domestic_etf",
                        "source_record_keys": ["KR7069500007"],
                        "source_fields": ["canonical_products.asset_manager"],
                    }
                ],
            }
        ]


def _step() -> QueryStep:
    return QueryStep(
        step_id="graph-relations",
        source=RetrievalSource.GRAPH,
        operation=QueryOperation.RELATIONSHIP_SEARCH,
        inputs={
            "paths": [
                {
                    "relations": ["managedBy"],
                    "directions": ["outgoing"],
                }
            ],
            "source_node_ids": ["etf_kr:KR7069500007"],
        },
    )


def test_real_graph_retriever_preserves_path_and_source_provenance() -> None:
    backend = StubGraphBackend()
    retriever = RealGraphRetriever(
        backend,  # type: ignore[arg-type]
        GraphQueryCompiler(GraphMappingRegistry(), snapshot="2026-07-11"),
        snapshot="2026-07-11",
    )
    step = _step()
    context = ExecutionContext(plan=QueryPlan(planner="supervisor", steps=[step]))

    records = asyncio.run(retriever.retrieve(step, context))

    assert len(records) == 1
    assert records[0].entity_id == "etf_kr:KR7069500007"
    assert records[0].payload == {
        "field": "graph.managedBy",
        "value": "삼성",
        "text": "KODEX 200 -> 삼성",
    }
    assert records[0].metadata["path_provenance"][0]["source_record_keys"] == [
        "KR7069500007"
    ]


def test_stale_graph_fails_closed_as_retriever_unavailable() -> None:
    retriever = RealGraphRetriever(
        StubGraphBackend(stale=True),  # type: ignore[arg-type]
        GraphQueryCompiler(GraphMappingRegistry(), snapshot="2026-07-11"),
        snapshot="2026-07-11",
    )
    step = _step()
    context = ExecutionContext(plan=QueryPlan(planner="supervisor", steps=[step]))

    with pytest.raises(RetrieverUnavailableError, match="snapshot mismatch"):
        asyncio.run(retriever.retrieve(step, context))


def test_empty_graph_match_is_a_normal_empty_result() -> None:
    retriever = RealGraphRetriever(
        StubGraphBackend(empty=True),  # type: ignore[arg-type]
        GraphQueryCompiler(GraphMappingRegistry(), snapshot="2026-07-11"),
        snapshot="2026-07-11",
    )
    step = _step()
    context = ExecutionContext(plan=QueryPlan(planner="supervisor", steps=[step]))

    assert asyncio.run(retriever.retrieve(step, context)) == []
