"""canonical_v2-only Neo4j projection and retrieval boundary.

This module intentionally does not reuse the legacy graph extractor.  It
reads canonical_v2 entities and approved facts only; source assertions are
attached to relationship provenance through fact ids and never create graph
semantics.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase
from sqlalchemy import Engine, func, select

from app.data.v2_rebuild import relation_domain_violations
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    canonical_facts,
    entity_classifications,
    entity_relations,
    fact_evidence_links,
    financial_products,
    holding_fact_details,
    index_relations,
    indices,
    ontology_concepts,
    organization_relations,
    organizations,
    securities,
    source_field_assertions,
    source_records,
)
from app.derived.manifest import DerivedStoreManifest, DerivedStoreStatus
from app.graph.config import GraphSettings
from app.graph.models import GraphBuildData, GraphBuildStats, GraphEdge, GraphNode


V2_GRAPH_NODE_LABEL = "M108DNode"
V2_GRAPH_METADATA_LABEL = "M108DDerivedStoreMetadata"
V2_GRAPH_METADATA_KEY = "canonical-v2-graph"
V2_GRAPH_PROJECTION_VERSION = "m10.9-c2.8-canonical-v2-graph-5"
V2_TRANSFORMER_VERSION = "m10.9-c2-kodex-holdings-1"

V2_RELATIONS = frozenset(
    {
        "HAS_SHARE_CLASS", "HAS_SALE_LOT", "MANAGED_BY", "ISSUED_BY",
        "HAS_TRUSTEE", "HAS_UNDERLYING_INDEX", "TRACKS_INDEX",
        "HAS_BENCHMARK", "DENOMINATED_IN", "TRADED_IN_CURRENCY",
        "LISTED_IN_COUNTRY", "HAS_INSTRUMENT_COUNTRY", "HAS_ASSET_CLASS",
        "HAS_EXPOSURE_REGION", "HAS_MARKET_SCOPE", "HAS_RISK_GRADE",
        "HAS_BOND_TYPE", "HAS_OFFERING_TYPE", "HOLDS",
        "SECURITY_ISSUED_BY",
    }
)

_TYPE_LABELS = {
    "ETF": "ETF", "ETN": "ETN", "BOND": "Bond", "FUND": "Fund",
    "FUND_SHARE_CLASS": "FundShareClass", "SALE_LOT": "SaleLot",
    "ORGANIZATION": "Organization", "INDEX": "Index", "CURRENCY": "Currency",
    "COUNTRY": "Country", "SECURITY": "EquitySecurity",
    "asset_class": "AssetClass", "exposure_region": "ExposureRegion",
    "market_scope": "MarketScope", "risk_grade": "RiskGrade",
    "bond_type": "BondType", "offering_type": "OfferingType",
}


def v2_manifest_checksum(
    entities: Mapping[str, int], relations: Mapping[str, int], *, document_count: int = 0
) -> str:
    payload = json.dumps(
        {"entities": dict(sorted(entities.items())), "relations": dict(sorted(relations.items())), "documents": document_count},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CanonicalV2GraphExtractor:
    """Deterministically turn one canonical_v2 snapshot into graph rows."""

    def __init__(
        self,
        engine: Engine,
        *,
        snapshot_ids: Iterable[str],
        snapshot: str,
        generation: str = "260824",
        ontology_version: str = "merged-optical-1.4",
        transformer_version: str = V2_TRANSFORMER_VERSION,
        projection_version: str = V2_GRAPH_PROJECTION_VERSION,
    ) -> None:
        self._engine = engine
        self._snapshot_ids = tuple(sorted(set(snapshot_ids)))
        if not self._snapshot_ids:
            raise ValueError("canonical_v2 graph projection requires snapshot ids")
        self._snapshot = snapshot
        self._generation = generation
        self._ontology_version = ontology_version
        self._transformer_version = transformer_version
        self._projection_version = projection_version
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._evidence_by_fact: dict[str, list[str]] = defaultdict(list)
        self._source_fields_by_fact: dict[str, list[str]] = defaultdict(list)
        self._source_record_keys_by_fact: dict[str, list[str]] = defaultdict(list)
        self._holding_by_fact: dict[str, Mapping[str, Any]] = {}
        self._stats = GraphBuildStats()

    def extract(self) -> GraphBuildData:
        with self._engine.connect() as connection:
            if relation_domain_violations(connection):
                raise ValueError("canonical_v2 relation-domain validation failed")
            product_type = financial_products.c.product_type_code
            rows = connection.execute(
                select(
                    canonical_entities.c.entity_id, canonical_entities.c.entity_kind,
                    canonical_entities.c.preferred_name, canonical_entities.c.query_eligible,
                    product_type, securities.c.ticker, securities.c.isin,
                    securities.c.exchange,
                )
                .select_from(
                    canonical_entities
                    .outerjoin(
                        financial_products,
                        financial_products.c.product_id == canonical_entities.c.entity_id,
                    )
                    .outerjoin(
                        securities,
                        securities.c.security_id == canonical_entities.c.entity_id,
                    )
                )
                .order_by(canonical_entities.c.entity_id)
            ).mappings()
            for row in rows:
                self._entity_node(row)
            for row in connection.execute(
                select(ontology_concepts).where(ontology_concepts.c.active.is_(True)).order_by(ontology_concepts.c.concept_iri)
            ).mappings():
                self._concept_node(row)
            self._extract_relation_metadata(connection)
            self._extract_entity_relations(connection)
            self._extract_organization_relations(connection)
            self._extract_index_relations(connection)
            self._extract_classifications(connection)
        self._stats.nodes_by_type = dict(sorted(Counter(node.node_type for node in self._nodes.values()).items()))
        self._stats.edges_by_relation = dict(sorted(Counter(edge.edge_type for edge in self._edges.values()).items()))
        return GraphBuildData(
            nodes=tuple(self._nodes[key] for key in sorted(self._nodes)),
            edges=tuple(self._edges[key] for key in sorted(self._edges)),
            stats=self._stats,
        )

    def manifest(self, data: GraphBuildData, *, status: DerivedStoreStatus) -> DerivedStoreManifest:
        return DerivedStoreManifest(
            store_kind="neo4j_graph",
            status=status,
            generation=self._generation,
            snapshot=self._snapshot,
            ontology_version=self._ontology_version,
            canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
            transformer_version=self._transformer_version,
            projection_version=self._projection_version,
            entity_counts=data.stats.nodes_by_type,
            relation_counts=data.stats.edges_by_relation,
            checksum=v2_manifest_checksum(data.stats.nodes_by_type, data.stats.edges_by_relation),
            validation={"source": "canonical_v2", "domain_validation": "passed"},
        )

    def _entity_node(self, row: Mapping[str, Any]) -> None:
        kind = str(row["entity_kind"])
        product = row["product_type_code"]
        node_type = _TYPE_LABELS.get(str(product)) if product is not None else _TYPE_LABELS.get(kind)
        if node_type is None:
            return
        labels = (
            (V2_GRAPH_NODE_LABEL, "FinancialProduct", node_type)
            if product is not None
            else (V2_GRAPH_NODE_LABEL, "Security", node_type)
            if kind == "SECURITY"
            else (V2_GRAPH_NODE_LABEL, node_type)
        )
        self._nodes[str(row["entity_id"])] = GraphNode(
            entity_id=str(row["entity_id"]), node_type=node_type, labels=labels,
            properties={
                "display_name": row["preferred_name"], "canonical_value": row["preferred_name"],
                "identifier_value": row["ticker"], "ticker": row["ticker"],
                "isin": row["isin"], "exchange": row["exchange"],
                "entity_kind": kind, "product_type": product,
                "node_type": node_type,
                "query_eligible": bool(row["query_eligible"]), "dataset_snapshot": self._snapshot,
            },
        )

    def _concept_node(self, row: Mapping[str, Any]) -> None:
        category = str(row["concept_category"])
        node_type = _TYPE_LABELS.get(category)
        if node_type is None:
            return
        identifier = str(row["concept_iri"])
        self._nodes[identifier] = GraphNode(
            entity_id=identifier, node_type=node_type, labels=(V2_GRAPH_NODE_LABEL, node_type),
            properties={
                "display_name": row["canonical_name"], "canonical_value": row["canonical_name"],
                "concept_iri": identifier, "concept_category": category,
                "node_type": node_type,
                "dataset_snapshot": self._snapshot,
            },
        )

    def _add_edge(self, *, fact_id: str, subject: str, relation: str, object_id: str) -> None:
        if relation not in V2_RELATIONS:
            raise ValueError(f"unapproved canonical_v2 graph relation: {relation}")
        if subject not in self._nodes or object_id not in self._nodes:
            raise ValueError(f"canonical relation has an unprojectable endpoint: {fact_id}")
        holding = self._holding_by_fact.get(fact_id)
        properties: dict[str, Any] = {
            "canonical_fact_id": fact_id, "dataset_snapshot": self._snapshot,
            "generation": self._generation, "ontology_version": self._ontology_version,
            "evidence_assertion_ids": sorted(self._evidence_by_fact.get(fact_id, [])),
            "source_fields": sorted(set(self._source_fields_by_fact.get(fact_id, []))),
            "source_record_keys": sorted(
                set(self._source_record_keys_by_fact.get(fact_id, []))
            ),
        }
        if holding is not None:
            properties.update({
                "effective_date": holding["effective_date"].isoformat(),
                "external_holding_record_id": holding["external_holding_record_id"],
                "source_provider": holding["source_provider"],
                "weight_normalized": (
                    str(holding["weight_normalized"])
                    if holding["weight_normalized"] is not None else None
                ),
                "weight_unit": holding["weight_unit"],
                "weight_scale": holding["weight_scale"],
            })
        self._edges[fact_id] = GraphEdge(
            edge_id=f"v2:{fact_id}", subject_id=subject, edge_type=relation, object_id=object_id,
            properties=properties,
        )

    def _extract_relation_metadata(self, connection) -> None:
        rows = connection.execute(
            select(
                fact_evidence_links.c.fact_id,
                fact_evidence_links.c.assertion_id,
                source_field_assertions.c.source_column,
                source_records.c.source_primary_key,
            )
            .join(canonical_facts, canonical_facts.c.fact_id == fact_evidence_links.c.fact_id)
            .join(
                source_field_assertions,
                source_field_assertions.c.assertion_id
                == fact_evidence_links.c.assertion_id,
            )
            .join(
                source_records,
                source_records.c.source_record_id
                == source_field_assertions.c.source_record_id,
            )
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
        )
        for fact_id, assertion_id, source_field, source_record_key in rows:
            self._evidence_by_fact[str(fact_id)].append(str(assertion_id))
            self._source_fields_by_fact[str(fact_id)].append(str(source_field))
            self._source_record_keys_by_fact[str(fact_id)].append(
                str(source_record_key)
            )
        rows = connection.execute(
            select(holding_fact_details)
            .join(canonical_facts, canonical_facts.c.fact_id == holding_fact_details.c.fact_id)
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
        ).mappings()
        self._holding_by_fact = {str(row["fact_id"]): row for row in rows}

    def _extract_entity_relations(self, connection) -> None:
        rows = connection.execute(
            select(entity_relations.c.fact_id, entity_relations.c.subject_entity_id, entity_relations.c.relation_type, entity_relations.c.object_entity_id)
            .join(canonical_facts, canonical_facts.c.fact_id == entity_relations.c.fact_id)
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
            .order_by(entity_relations.c.fact_id)
        ).mappings()
        for row in rows:
            self._add_edge(fact_id=str(row["fact_id"]), subject=str(row["subject_entity_id"]), relation=str(row["relation_type"]), object_id=str(row["object_entity_id"]))

    def _extract_organization_relations(self, connection) -> None:
        rows = connection.execute(
            select(organization_relations.c.fact_id, organization_relations.c.subject_product_id, organization_relations.c.relation_type, organization_relations.c.organization_id)
            .join(canonical_facts, canonical_facts.c.fact_id == organization_relations.c.fact_id)
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
            .order_by(organization_relations.c.fact_id)
        ).mappings()
        for row in rows:
            self._add_edge(fact_id=str(row["fact_id"]), subject=str(row["subject_product_id"]), relation=str(row["relation_type"]), object_id=str(row["organization_id"]))

    def _extract_index_relations(self, connection) -> None:
        rows = connection.execute(
            select(index_relations.c.fact_id, index_relations.c.subject_product_id, index_relations.c.relation_type, index_relations.c.index_id)
            .join(canonical_facts, canonical_facts.c.fact_id == index_relations.c.fact_id)
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
            .order_by(index_relations.c.fact_id)
        ).mappings()
        for row in rows:
            self._add_edge(fact_id=str(row["fact_id"]), subject=str(row["subject_product_id"]), relation=str(row["relation_type"]), object_id=str(row["index_id"]))

    def _extract_classifications(self, connection) -> None:
        rows = connection.execute(
            select(entity_classifications.c.fact_id, entity_classifications.c.entity_id, entity_classifications.c.classification_type, entity_classifications.c.concept_iri)
            .join(canonical_facts, canonical_facts.c.fact_id == entity_classifications.c.fact_id)
            .where(canonical_facts.c.snapshot_id.in_(self._snapshot_ids))
            .order_by(entity_classifications.c.fact_id)
        ).mappings()
        for row in rows:
            self._add_edge(fact_id=str(row["fact_id"]), subject=str(row["entity_id"]), relation="HAS_" + str(row["classification_type"]), object_id=str(row["concept_iri"]))


class CanonicalV2GraphBackend:
    """Isolated Neo4j namespace for a canonical_v2-derived graph."""

    def __init__(self, driver: AsyncDriver, settings: GraphSettings) -> None:
        self._driver = driver
        self._settings = settings

    @classmethod
    def connect(cls, settings: GraphSettings) -> "CanonicalV2GraphBackend":
        if not settings.configured:
            raise ValueError("NEO4J_URI and NEO4J_PASSWORD are required")
        return cls(
            AsyncGraphDatabase.driver(
                settings.uri,
                auth=(settings.user, settings.password),
                connection_timeout=settings.connection_timeout_seconds,
                connection_acquisition_timeout=(
                    settings.connection_acquisition_timeout_seconds
                ),
                max_transaction_retry_time=settings.max_transaction_retry_seconds,
            ),
            settings,
        )

    async def close(self) -> None:
        await self._driver.close()

    async def verify_connectivity(self) -> None:
        await self._driver.verify_connectivity()

    async def build(self, data: GraphBuildData, manifest: DerivedStoreManifest) -> DerivedStoreManifest:
        if manifest.store_kind != "neo4j_graph":
            raise ValueError("graph manifest has the wrong store kind")
        await self._constraints()
        building = manifest.model_copy(update={"status": DerivedStoreStatus.BUILDING})
        await self._write_manifest(building)
        try:
            await self._clear()
            await self._write_nodes(data.nodes)
            await self._write_edges(data.edges)
            actual_nodes, actual_edges = await self.counts(snapshot=manifest.snapshot)
            if actual_nodes != data.stats.total_nodes or actual_edges != data.stats.total_edges:
                raise RuntimeError("canonical_v2 graph count reconciliation failed")
            actual_relations = await self.relation_counts(snapshot=manifest.snapshot)
            if actual_relations != data.stats.edges_by_relation:
                raise RuntimeError("canonical_v2 graph relation reconciliation failed")
            ready = manifest.model_copy(update={
                "status": DerivedStoreStatus.READY,
                "validation": {**manifest.validation, "node_count": actual_nodes, "edge_count": actual_edges, "relation_reconciliation": "passed"},
            })
            await self._write_manifest(ready)
            return ready
        except Exception as exc:
            await self._write_manifest(manifest.model_copy(update={
                "status": DerivedStoreStatus.FAILED,
                "validation": {**manifest.validation, "error": str(exc)},
            }))
            raise

    async def manifest(self) -> DerivedStoreManifest | None:
        rows = await self._execute(
            f"MATCH (m:{V2_GRAPH_METADATA_LABEL} {{metadata_key: $key}}) RETURN m.manifest_json AS manifest_json",
            {"key": V2_GRAPH_METADATA_KEY},
        )
        return DerivedStoreManifest.model_validate_json(str(rows[0]["manifest_json"])) if rows else None

    async def assert_ready(self, *, expected_snapshot: str) -> DerivedStoreManifest:
        manifest = await self.manifest()
        if manifest is None:
            raise RuntimeError("canonical_v2 graph manifest is missing")
        manifest.assert_compatible(
            generation=self._settings.v2_generation, snapshot=expected_snapshot,
            ontology_version=self._settings.v2_ontology_version,
            canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
            transformer_version=self._settings.v2_transformer_version,
            projection_version=self._settings.v2_graph_projection_version,
        )
        return manifest

    async def query(self, cypher: str, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
        return await self._execute(cypher, dict(parameters))

    async def counts(self, *, snapshot: str) -> tuple[int, int]:
        nodes = await self._execute(f"MATCH (n:{V2_GRAPH_NODE_LABEL} {{dataset_snapshot: $snapshot}}) RETURN count(n) AS count", {"snapshot": snapshot})
        edges = await self._execute(f"MATCH (:{V2_GRAPH_NODE_LABEL})-[r]->(:{V2_GRAPH_NODE_LABEL}) WHERE r.dataset_snapshot = $snapshot RETURN count(r) AS count", {"snapshot": snapshot})
        return int(nodes[0]["count"]), int(edges[0]["count"])

    async def relation_counts(self, *, snapshot: str) -> dict[str, int]:
        rows = await self._execute(f"MATCH (:{V2_GRAPH_NODE_LABEL})-[r]->(:{V2_GRAPH_NODE_LABEL}) WHERE r.dataset_snapshot = $snapshot RETURN type(r) AS relation, count(r) AS count", {"snapshot": snapshot})
        return {str(row["relation"]): int(row["count"]) for row in rows}

    async def _constraints(self) -> None:
        await self._execute(f"CREATE CONSTRAINT m108d_entity_id IF NOT EXISTS FOR (n:{V2_GRAPH_NODE_LABEL}) REQUIRE n.entity_id IS UNIQUE", {})
        await self._execute(f"CREATE CONSTRAINT m108d_metadata_key IF NOT EXISTS FOR (m:{V2_GRAPH_METADATA_LABEL}) REQUIRE m.metadata_key IS UNIQUE", {})

    async def _clear(self) -> None:
        for query in (
            f"MATCH (:{V2_GRAPH_NODE_LABEL})-[r]->(:{V2_GRAPH_NODE_LABEL}) WITH r LIMIT $batch DELETE r RETURN count(r) AS deleted",
            f"MATCH (n:{V2_GRAPH_NODE_LABEL}) WITH n LIMIT $batch DETACH DELETE n RETURN count(n) AS deleted",
        ):
            while True:
                rows = await self._execute(query, {"batch": self._settings.batch_size})
                if int(rows[0]["deleted"]) < self._settings.batch_size:
                    break

    async def _write_nodes(self, nodes: Iterable[GraphNode]) -> None:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for node in nodes:
            if V2_GRAPH_NODE_LABEL not in node.labels:
                raise ValueError("canonical_v2 node is missing its isolation label")
            groups[node.labels].append({"entity_id": node.entity_id, "properties": {**node.properties, "entity_id": node.entity_id, "projection_version": self._settings.v2_graph_projection_version}})
        for labels, rows in groups.items():
            label_fragment = ":".join(labels)
            query = f"UNWIND $rows AS row MERGE (n:{label_fragment} {{entity_id: row.entity_id}}) SET n += row.properties"
            for batch in _batches(rows, self._settings.batch_size):
                await self._execute(query, {"rows": batch})

    async def _write_edges(self, edges: Iterable[GraphEdge]) -> None:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            if edge.edge_type not in V2_RELATIONS:
                raise ValueError(f"unapproved canonical_v2 graph relation: {edge.edge_type}")
            groups[edge.edge_type].append({"edge_id": edge.edge_id, "subject_id": edge.subject_id, "object_id": edge.object_id, "properties": {**edge.properties, "edge_id": edge.edge_id, "edge_type": edge.edge_type, "projection_version": self._settings.v2_graph_projection_version}})
        for relation, rows in groups.items():
            query = f"UNWIND $rows AS row MATCH (s:{V2_GRAPH_NODE_LABEL} {{entity_id: row.subject_id}}) MATCH (o:{V2_GRAPH_NODE_LABEL} {{entity_id: row.object_id}}) MERGE (s)-[r:{relation} {{edge_id: row.edge_id}}]->(o) SET r += row.properties"
            for batch in _batches(rows, self._settings.batch_size):
                await self._execute(query, {"rows": batch})

    async def _write_manifest(self, manifest: DerivedStoreManifest) -> None:
        await self._execute(
            f"MERGE (m:{V2_GRAPH_METADATA_LABEL} {{metadata_key: $key}}) SET m.manifest_json = $manifest_json",
            {"key": V2_GRAPH_METADATA_KEY, "manifest_json": manifest.model_dump_json()},
        )

    async def _execute(self, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        records, _, _ = await self._driver.execute_query(query, parameters_=parameters, database_=self._settings.database)
        return [record.data() for record in records]


def _batches(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]
