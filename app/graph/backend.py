from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.graph.config import GraphSettings
from app.graph.mapping import GRAPH_NODE_LABELS, GraphMappingRegistry
from app.graph.models import GraphBuildData, GraphMetadata, GraphNode


class Neo4jGraphBackend:
    """Neo4j adapter used by both offline ingestion and online retrieval."""

    def __init__(self, driver: AsyncDriver, settings: GraphSettings) -> None:
        self._driver = driver
        self._settings = settings

    @classmethod
    def connect(cls, settings: GraphSettings) -> "Neo4jGraphBackend":
        if not settings.configured or settings.uri is None:
            raise ValueError("NEO4J_URI and NEO4J_PASSWORD are required")
        driver = AsyncGraphDatabase.driver(
            settings.uri,
            auth=(settings.user, settings.password),
        )
        return cls(driver, settings)

    async def verify_connectivity(self) -> None:
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        await self._driver.close()

    async def build(
        self,
        data: GraphBuildData,
        *,
        dataset_snapshot: str,
    ) -> GraphMetadata:
        await self._create_constraints()
        built_at = datetime.now(UTC)
        await self._write_metadata(
            dataset_snapshot,
            built_at,
            status="building",
            statistics=data.stats.as_dict(),
        )
        await self._clear_entities()
        await self._write_nodes(data.nodes)
        await self._write_edges(data)
        actual = await self.counts(dataset_snapshot)
        expected = {
            "total_nodes": data.stats.total_nodes,
            "total_edges": data.stats.total_edges,
        }
        if actual != expected:
            await self._write_metadata(
                dataset_snapshot,
                built_at,
                status="failed",
                statistics={**data.stats.as_dict(), "actual": actual},
            )
            raise RuntimeError(
                f"Neo4j graph count mismatch: expected={expected}, actual={actual}"
            )
        statistics = {**data.stats.as_dict(), "actual": actual}
        await self._write_metadata(
            dataset_snapshot,
            built_at,
            status="ready",
            statistics=statistics,
        )
        return GraphMetadata(
            graph_version=self._settings.graph_version,
            dataset_snapshot=dataset_snapshot,
            built_at=built_at,
            status="ready",
            statistics=statistics,
        )

    async def metadata(self) -> GraphMetadata | None:
        records = await self._execute(
            """
            MATCH (m:M10GraphMetadata {metadata_key: $metadata_key})
            RETURN m.graph_version AS graph_version,
                   m.dataset_snapshot AS dataset_snapshot,
                   m.built_at AS built_at,
                   m.status AS status,
                   m.statistics_json AS statistics_json
            """,
            {"metadata_key": self._metadata_key},
        )
        if not records:
            return None
        row = records[0]
        return GraphMetadata(
            graph_version=str(row["graph_version"]),
            dataset_snapshot=str(row["dataset_snapshot"]),
            built_at=datetime.fromisoformat(str(row["built_at"])),
            status=str(row["status"]),
            statistics=json.loads(str(row["statistics_json"])),
        )

    async def assert_ready(self, *, expected_snapshot: str) -> GraphMetadata:
        metadata = await self.metadata()
        if metadata is None:
            raise RuntimeError("graph metadata is missing; run app.graph.ingest")
        if metadata.status != "ready":
            raise RuntimeError(f"graph is not ready: status={metadata.status}")
        if metadata.graph_version != self._settings.graph_version:
            raise RuntimeError(
                "graph version mismatch: "
                f"expected={self._settings.graph_version}, "
                f"actual={metadata.graph_version}"
            )
        if metadata.dataset_snapshot != expected_snapshot:
            raise RuntimeError(
                "graph snapshot mismatch: "
                f"expected={expected_snapshot}, "
                f"actual={metadata.dataset_snapshot}"
            )
        return metadata

    async def query(
        self,
        cypher: str,
        parameters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return await self._execute(cypher, dict(parameters))

    async def counts(self, snapshot: str) -> dict[str, int]:
        node_rows = await self._execute(
            "MATCH (n:M10Entity {dataset_snapshot: $snapshot}) "
            "RETURN count(n) AS count",
            {"snapshot": snapshot},
        )
        edge_rows = await self._execute(
            "MATCH (:M10Entity)-[r]->(:M10Entity) "
            "WHERE r.dataset_snapshot = $snapshot RETURN count(r) AS count",
            {"snapshot": snapshot},
        )
        return {
            "total_nodes": int(node_rows[0]["count"]),
            "total_edges": int(edge_rows[0]["count"]),
        }

    async def _create_constraints(self) -> None:
        await self._execute(
            "CREATE CONSTRAINT m10_entity_id IF NOT EXISTS "
            "FOR (n:M10Entity) REQUIRE n.entity_id IS UNIQUE",
            {},
        )

    async def _clear_entities(self) -> None:
        """Bound rebuild deletion memory by committing each small batch."""
        edge_query = (
            "MATCH (:M10Entity)-[r]->(:M10Entity) WITH r LIMIT $batch "
            "DELETE r RETURN count(r) AS deleted"
        )
        node_query = (
            "MATCH (n:M10Entity) WITH n LIMIT $batch "
            "DELETE n RETURN count(n) AS deleted"
        )
        for query in (edge_query, node_query):
            while True:
                rows = await self._execute(
                    query, {"batch": self._settings.batch_size}
                )
                deleted = int(rows[0]["deleted"])
                if deleted < self._settings.batch_size:
                    break
        await self._execute(
            "CREATE CONSTRAINT m10_metadata_key IF NOT EXISTS "
            "FOR (m:M10GraphMetadata) REQUIRE m.metadata_key IS UNIQUE",
            {},
        )

    async def _write_nodes(self, nodes: Iterable[GraphNode]) -> None:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            if not set(node.labels).issubset(GRAPH_NODE_LABELS):
                raise ValueError(f"unsupported graph labels: {node.labels}")
            groups[node.labels].append(
                {
                    "entity_id": node.entity_id,
                    "properties": {
                        **node.properties,
                        "entity_id": node.entity_id,
                        "graph_version": self._settings.graph_version,
                    },
                }
            )
        for labels, rows in groups.items():
            label_fragment = ":".join(labels)
            query = (
                f"UNWIND $rows AS row "
                f"MERGE (n:{label_fragment} {{entity_id: row.entity_id}}) "
                "SET n += row.properties"
            )
            for batch in _batches(rows, self._settings.batch_size):
                await self._execute(query, {"rows": batch})

    async def _write_edges(self, data: GraphBuildData) -> None:
        registry = GraphMappingRegistry()
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in data.edges:
            registry.by_edge(edge.edge_type)
            groups[edge.edge_type].append(
                {
                    "edge_id": edge.edge_id,
                    "subject_id": edge.subject_id,
                    "object_id": edge.object_id,
                    "properties": {
                        **edge.properties,
                        "edge_id": edge.edge_id,
                        "graph_version": self._settings.graph_version,
                    },
                }
            )
        for edge_type, rows in groups.items():
            query = (
                "UNWIND $rows AS row "
                "MATCH (s:M10Entity {entity_id: row.subject_id}) "
                "MATCH (o:M10Entity {entity_id: row.object_id}) "
                f"MERGE (s)-[r:{edge_type} {{edge_id: row.edge_id}}]->(o) "
                "SET r += row.properties"
            )
            for batch in _batches(rows, self._settings.batch_size):
                await self._execute(query, {"rows": batch})

    async def _write_metadata(
        self,
        snapshot: str,
        built_at: datetime,
        *,
        status: str,
        statistics: dict[str, Any],
    ) -> None:
        await self._execute(
            """
            MERGE (m:M10GraphMetadata {metadata_key: $metadata_key})
            SET m.graph_version = $graph_version,
                m.dataset_snapshot = $dataset_snapshot,
                m.built_at = $built_at,
                m.status = $status,
                m.statistics_json = $statistics_json
            """,
            {
                "metadata_key": self._metadata_key,
                "graph_version": self._settings.graph_version,
                "dataset_snapshot": snapshot,
                "built_at": built_at.isoformat(),
                "status": status,
                "statistics_json": json.dumps(
                    statistics, ensure_ascii=False, sort_keys=True
                ),
            },
        )

    @property
    def _metadata_key(self) -> str:
        return "financial-agent-m10"

    async def _execute(
        self,
        query: str,
        parameters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(
            query,
            parameters_=parameters,
            database_=self._settings.database,
        )
        return [record.data() for record in records]


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]
