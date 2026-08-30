"""Opt-in PostgreSQL/Neo4j acceptance coverage for M10.8-D.

The test never creates a v1 graph or semantic artifact.  It verifies only
the separately configured canonical_v2-derived stores.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url

from app.data.v2_schema import (
    canonical_facts,
    dataset_snapshots,
    entity_classifications,
    entity_relations,
    index_relations,
    organization_relations,
)
from app.graph.config import GraphSettings
from app.graph.compiler import GraphQueryCompiler
from app.graph.mapping import GraphMappingRegistry
from app.graph.v2 import CanonicalV2GraphBackend, CanonicalV2GraphExtractor
from app.domain.models import QueryOperation, QueryStep, RetrievalSource
from app.retrieval.exceptions import GraphQueryCompilationError
from app.retrieval.rdb_v2 import CanonicalV2SnapshotSelector
from app.search.config import SearchSettings
from app.search.embedding import DeterministicMultilingualEmbeddingProvider
from app.search.indexer import SemanticIndexBuilder
from app.search.store import SemanticIndexStore
from app.search.v2 import (
    CanonicalV2StrategyDocumentBuilder,
    V2_SEMANTIC_PROJECTION_VERSION,
    v2_semantic_manifest_factory,
)


pytestmark = pytest.mark.postgresql


def test_v2_graph_compiler_isolated_and_allow_listed() -> None:
    compiler = GraphQueryCompiler(
        GraphMappingRegistry(version="canonical-v2"),
        snapshot="2026-08-24", node_label="M108DNode",
    )
    step = QueryStep(
        step_id="graph", source=RetrievalSource.GRAPH,
        operation=QueryOperation.RELATIONSHIP_SEARCH,
        inputs={"source_node_ids": ["fund:one"]},
    )
    compiled = compiler.compile(
        step, {"relations": ["hasShareClass"], "directions": ["outgoing"]}
    )
    assert "M108DNode" in compiled.cypher
    assert "HAS_SHARE_CLASS" in compiled.cypher
    assert "count(path) AS total_matches" in compiled.count_cypher
    with pytest.raises(GraphQueryCompilationError):
        compiler.compile(step, {"relations": ["raw user Cypher"], "directions": ["outgoing"]})


def _database_url() -> str:
    value = os.getenv("M10_8_D_DATABASE_URL")
    if not value:
        pytest.skip("M10_8_D_DATABASE_URL is not configured")
    if make_url(value).get_backend_name() != "postgresql":
        pytest.fail("M10_8_D_DATABASE_URL must use PostgreSQL")
    if "test" not in (make_url(value).database or "").casefold() and "m108d" not in (make_url(value).database or "").casefold():
        pytest.fail("M10.8-D acceptance requires a disposable test/m108d database")
    return value


@pytest.fixture(scope="module")
def engine():
    result = create_engine(_database_url(), future=True)
    with result.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(dataset_snapshots).where(dataset_snapshots.c.status == "READY")) == 4
    yield result
    result.dispose()


@pytest.fixture(scope="module")
def selection(engine):
    with engine.connect() as connection:
        return CanonicalV2SnapshotSelector(snapshot_date="2026-08-24").select(connection)


def _canonical_relation_counts(engine, snapshot_ids: tuple[str, ...]) -> dict[str, int]:
    statements = (
        select(entity_relations.c.relation_type, func.count()).join(canonical_facts).where(canonical_facts.c.snapshot_id.in_(snapshot_ids)).group_by(entity_relations.c.relation_type),
        select(organization_relations.c.relation_type, func.count()).join(canonical_facts).where(canonical_facts.c.snapshot_id.in_(snapshot_ids)).group_by(organization_relations.c.relation_type),
        select(index_relations.c.relation_type, func.count()).join(canonical_facts).where(canonical_facts.c.snapshot_id.in_(snapshot_ids)).group_by(index_relations.c.relation_type),
        select(("HAS_" + entity_classifications.c.classification_type), func.count()).join(canonical_facts).where(canonical_facts.c.snapshot_id.in_(snapshot_ids)).group_by(entity_classifications.c.classification_type),
    )
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for statement in statements:
            counts.update({str(key): int(value) for key, value in connection.execute(statement)})
    return counts


def test_v2_graph_extractor_reconciles_and_preserves_grain(engine, selection) -> None:
    data = CanonicalV2GraphExtractor(
        engine, snapshot_ids=selection.snapshot_ids, snapshot="2026-08-24"
    ).extract()
    assert data.stats.edges_by_relation == _canonical_relation_counts(engine, selection.snapshot_ids)
    forbidden = {
        "MANAGED_BY": "FundShareClass",
        "HAS_TRUSTEE": "FundShareClass",
        "HAS_BENCHMARK": "FundShareClass",
        "DENOMINATED_IN": "FundShareClass",
    }
    nodes = {node.entity_id: node for node in data.nodes}
    assert all(node.properties["node_type"] == node.node_type for node in data.nodes)
    assert not any(
        edge.edge_type == relation and nodes[edge.subject_id].node_type == node_type
        for relation, node_type in forbidden.items()
        for edge in data.edges
    )
    assert not any(
        edge.edge_type == "MANAGED_BY" and nodes[edge.subject_id].node_type == "ETN"
        for edge in data.edges
    )
    assert data.stats.edges_by_relation["HAS_SHARE_CLASS"] == 16_574
    assert data.stats.edges_by_relation["HAS_SALE_LOT"] == 21_882
    assert data.stats.edges_by_relation["MANAGED_BY"] >= 6_863


def test_v2_semantic_documents_are_canonical_and_stable(engine, selection, tmp_path: Path) -> None:
    builder = CanonicalV2StrategyDocumentBuilder(
        engine, snapshot_ids=selection.snapshot_ids, snapshot_date="2026-08-24"
    )
    documents, _ = builder.build()
    assert documents
    assert all(item.document_id.startswith("v2:") for item in documents)
    assert all(item.metadata["source_record_id"] in item.document_id for item in documents)
    assert all(item.entity_id and item.metadata["canonical_v2"] for item in documents)
    settings = replace(
        SearchSettings(), index_path=tmp_path / "canonical_v2.json",
        index_version=V2_SEMANTIC_PROJECTION_VERSION,
    )
    provider = DeterministicMultilingualEmbeddingProvider(
        dimension=settings.embedding_dimension
    )
    first = asyncio.run(SemanticIndexBuilder(
        builder, provider, settings,
        derived_manifest_factory=v2_semantic_manifest_factory(
            generation="260824", snapshot="2026-08-24", ontology_version="merged-optical-1.3",
        ),
    ).build())
    second = asyncio.run(SemanticIndexBuilder(
        builder, provider, settings,
        derived_manifest_factory=v2_semantic_manifest_factory(
            generation="260824", snapshot="2026-08-24", ontology_version="merged-optical-1.3",
        ),
    ).build())
    assert first.metadata.document_count == second.metadata.document_count
    assert first.new_documents == first.metadata.document_count
    assert first.regenerated_embeddings == 0
    assert second.reused_embeddings == second.metadata.document_count
    assert second.new_documents == second.removed_documents == 0
    store = SemanticIndexStore(settings.index_path)
    manifest = store.validate_derived_manifest(
        generation="260824", snapshot="2026-08-24", ontology_version="merged-optical-1.3",
        canonical_schema_version="m10.8-b-canonical-v2",
        transformer_version="m10.8-b2-relations-v2",
        projection_version=V2_SEMANTIC_PROJECTION_VERSION,
    )
    assert manifest.document_count == len(documents)


@pytest.mark.neo4j
def test_v2_neo4j_projection_is_idempotent(engine, selection) -> None:
    uri = os.getenv("M10_8_D_NEO4J_URI")
    if not uri:
        pytest.skip("M10_8_D_NEO4J_URI is not configured")
    settings = GraphSettings(
        uri=uri, password=os.getenv("M10_8_D_NEO4J_PASSWORD", "test-password"),
        graph_version="m10.8-d-test", v2_graph_projection_version="m10.8-d-canonical-v2-graph-1",
    )
    extractor = CanonicalV2GraphExtractor(
        engine, snapshot_ids=selection.snapshot_ids, snapshot="2026-08-24"
    )
    data = extractor.extract()

    async def run() -> None:
        backend = CanonicalV2GraphBackend.connect(settings)
        try:
            first = await backend.build(data, extractor.manifest(data, status="BUILDING"))
            before = await backend.counts(snapshot="2026-08-24")
            second = await backend.build(data, extractor.manifest(data, status="BUILDING"))
            after = await backend.counts(snapshot="2026-08-24")
            assert first.status.value == second.status.value == "READY"
            assert before == after == (data.stats.total_nodes, data.stats.total_edges)
            assert await backend.relation_counts(snapshot="2026-08-24") == data.stats.edges_by_relation
        finally:
            await backend.close()
    asyncio.run(run())
