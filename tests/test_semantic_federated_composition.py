"""Operator composition using fixture IDs, bounded paths and real compilers."""
import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.domain.models import (
    CanonicalConcept, ComparisonSpec, ExecutionContext, FilterSpec,
    GroundedField, GroundedFilter, GroundedQuery, GroundedRelation, GroundedSort,
    GroundingStatus, ParsedQuery, ProductUniverseUnion, ResultLimit, SortSpec,
    QueryOperation, QueryPlan, QueryStep, RetrievalRecord, RetrievalSource,
    StepExecutionResult, StepExecutionStatus,
)
from app.execution.executor import QueryExecutor
from app.execution.transforms import InternalTransformExecutor
from app.graph.compiler import GraphQueryCompiler
from app.graph.mapping import GraphMappingRegistry
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.retrieval.exceptions import (
    GraphQueryCompilationError, IncompleteCandidateSetError, RetrieverUnavailableError,
)
from app.retrieval.graph import RealGraphRetriever
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry, CanonicalV2QueryCompiler, V2SnapshotSelection,
    _bind_dependency_candidates,
)
from app.retrieval.semantic import RealBM25Retriever, RealVectorRetriever
from app.search.config import SearchSettings
from app.search.models import SemanticDocument, SemanticSearchHit


SNAPSHOT = "2026-08-24"
GENERATION = "260824"


def _step(source, step_id=None, **inputs):
    return QueryStep(
        step_id=step_id or source,
        source=source,
        operation={
            "rdb": QueryOperation.SEARCH_PRODUCTS,
            "graph": QueryOperation.RELATIONSHIP_SEARCH,
            "bm25": QueryOperation.SEMANTIC_SEARCH,
            "vector": QueryOperation.SEMANTIC_SEARCH,
            "internal": QueryOperation.FILTER_CANDIDATES,
        }[source],
        inputs=inputs,
    )


def _record(source, entity, field, value, **metadata):
    return RetrievalRecord(
        step_id=source, source=source, source_id=f"{source}:{entity}:{field}",
        entity_id=entity, payload={"field": field, "value": value},
        metadata={"dataset_snapshot": SNAPSHOT, "generation": GENERATION, **metadata},
    )


def _result(step, records, **metadata):
    now = datetime.now(UTC)
    return StepExecutionResult(
        step_id=step.step_id, source=step.source, status=StepExecutionStatus.SUCCESS,
        records=records, started_at=now, finished_at=now, duration_seconds=0,
        retrieval_metadata={"counts": {"candidate_set_complete": 1}, **metadata},
    )


def _context(steps, results=()):
    return ExecutionContext(
        plan=QueryPlan(planner="supervisor", steps=steps),
        step_results={result.step_id: result for result in results},
    )


def _path(relation="holds"):
    return {"relations": [relation], "directions": ["outgoing"],
            "target_values": ["security:fixture"]}


class GraphBackend:
    def __init__(self, by_edge=None, total=None):
        self.by_edge = by_edge or {"HOLDS": ["etf:alpha", "etf:beta"]}
        self.total = total
        self.queries = []

    async def assert_ready(self, *, expected_snapshot):
        assert expected_snapshot == SNAPSHOT
        return SimpleNamespace(generation=GENERATION, projection_version="fixture")

    async def query(self, cypher, parameters):
        self.queries.append((cypher, parameters))
        edge = next(name for name in self.by_edge if f":{name}]" in cypher)
        entities = self.by_edge[edge]
        if "total_matches" in cypher:
            return [{"total_matches": self.total if self.total is not None else len(entities)}]
        rows = [{
            "nodes": [
                {"entity_id": entity, "display_name": entity},
                {"entity_id": "security:fixture", "display_name": "Fixture security"},
            ],
            "edges": [{
                "edge_id": f"fact:{edge}:{entity}", "edge_type": edge,
                "canonical_fact_id": f"fact:{edge}:{entity}",
                "evidence_assertion_ids": [f"assertion:{entity}"],
                "weight_normalized": "2.5", "weight_unit": "PERCENT",
                "weight_scale": "0_TO_100", "effective_date": SNAPSHOT,
            }],
        } for entity in entities[:parameters["limit"]]]
        if "SECURITY_ISSUED_BY" in cypher:
            for row in rows:
                row["nodes"].append({"entity_id": "organization:fixture", "display_name": "Fixture issuer"})
                row["edges"].append({
                    "edge_id": "issuer:fixture", "edge_type": "SECURITY_ISSUED_BY",
                    "canonical_fact_id": "issuer:fixture", "evidence_assertion_ids": ["assertion:issuer"],
                })
        return rows


def _graph(backend, *, limit=100):
    compiler = GraphQueryCompiler(
        GraphMappingRegistry(version="canonical-v2"), snapshot=SNAPSHOT,
        node_label="M108DNode", limit=limit,
    )
    return RealGraphRetriever(backend, compiler, snapshot=SNAPSHOT)


def test_graph_paths_compose_as_intersection_with_all_relation_evidence():
    backend = GraphBackend({
        "HOLDS": ["etf:alpha", "etf:beta"],
        "MANAGED_BY": ["etf:beta", "etf:gamma"],
    })
    step = _step("graph", paths=[_path(), _path("managedBy")],
                 path_operator="and", require_complete_candidates=True)
    result = asyncio.run(_graph(backend).retrieve_with_result(step, _context([step])))
    assert [record.entity_id for record in result.records] == ["etf:beta", "etf:beta"]
    assert {record.payload["field"] for record in result.records} == {
        "graph.holds", "graph.managedBy",
    }
    assert result.counts["candidate_set_complete"] == 1
    assert result.counts["candidate_count"] == 1
    assert result.total_matches is None  # no invented union path count


def test_graph_projection_keeps_actual_weight_and_manifest_identity():
    step = _step("graph", paths=[_path()])
    result = asyncio.run(_graph(GraphBackend()).retrieve_with_result(step, _context([step])))
    record = result.records[0]
    assert record.metadata["snapshot_identity"] == f"{GENERATION}:{SNAPSHOT}"
    provenance = record.metadata["path_provenance"][0]
    assert provenance["weight_normalized"] == "2.5"
    assert provenance["weight_unit"] == "PERCENT"
    assert provenance["weight_scale"] == "0_TO_100"
    assert provenance["evidence_assertion_ids"] == ["assertion:etf:alpha"]


def test_truncated_graph_window_cannot_feed_global_ranking():
    step = _step("graph", paths=[_path()], require_complete_candidates=True)
    with pytest.raises(IncompleteCandidateSetError, match="all matching paths"):
        asyncio.run(_graph(GraphBackend(), limit=1).retrieve_with_result(step, _context([step])))
    step.inputs["require_complete_candidates"] = False
    result = asyncio.run(_graph(GraphBackend(), limit=1).retrieve_with_result(step, _context([step])))
    assert result.counts["candidate_set_complete"] == 0
    assert result.total_matches == 2 and result.returned_count == 1


def test_empty_graph_dependency_never_becomes_unrestricted():
    rdb = _step("rdb")
    step = _step("graph", paths=[_path()], candidate_ids_from=["rdb"])
    step.depends_on = ["rdb"]
    backend = GraphBackend()
    result = asyncio.run(_graph(backend).retrieve_with_result(
        step, _context([rdb, step], [_result(rdb, [])]),
    ))
    assert result.records == [] and result.total_matches == 0
    assert result.counts["candidate_set_complete"] == 1
    assert backend.queries == []


def test_failed_graph_dependency_never_falls_back_to_target_anchor():
    step = _step("graph", paths=[_path()], candidate_ids_from=["rdb"])
    step.depends_on = ["rdb"]
    with pytest.raises(RetrieverUnavailableError, match="dependency"):
        asyncio.run(_graph(GraphBackend()).retrieve_with_result(step, _context([step])))


@pytest.mark.parametrize("relations,directions", [
    (["subsidiary"], ["outgoing"]),
    (["holds"], ["sideways"]),
    (["holds", "securityIssuedBy", "managedBy"], ["outgoing"] * 3),
])
def test_unknown_or_unbounded_relation_shapes_fail_closed(relations, directions):
    compiler = GraphQueryCompiler(GraphMappingRegistry(version="canonical-v2"), snapshot=SNAPSHOT)
    step = _step("graph", source_node_ids=["etf:fixture"])
    with pytest.raises(GraphQueryCompilationError):
        compiler.compile(step, {"relations": relations, "directions": directions})


def test_two_hop_compiler_keeps_typed_intermediate_and_parameterized_anchor():
    compiler = GraphQueryCompiler(GraphMappingRegistry(version="canonical-v2"), snapshot=SNAPSHOT)
    path = {
        "relations": ["holds", "securityIssuedBy"],
        "directions": ["outgoing", "outgoing"],
        "target_values": [None, "organization:fixture"],
        "target_types": ["EquitySecurity", "Organization"],
    }
    compiled = compiler.compile(_step("graph"), path)
    assert "HOLDS" in compiled.cypher and "SECURITY_ISSUED_BY" in compiled.cypher
    assert "n1.node_type = $target_type_0" in compiled.cypher
    assert compiled.parameters["target_value_1"] == "organization:fixture"
    assert "organization:fixture" not in compiled.cypher
    assert "r1.dataset_snapshot = $snapshot" in compiled.cypher


def test_filter_merge_preserves_entity_field_matrix_and_graph_paths():
    rdb, graph = _step("rdb"), _step("graph")
    merge = _step("internal", require_complete_candidates=True)
    merge.depends_on = ["rdb", "graph"]
    rdb_records = [
        _record("rdb", entity, field, value, real_rdb=True)
        for entity in ("etf:alpha", "etf:beta")
        for field, value in (("product.name", entity), ("product.aum", "100"))
    ]
    graph_records = [_record("graph", "etf:beta", "graph.holds", "security:fixture",
                             real_graph=True, path_provenance=[{"canonical_fact_id": "fact:beta"}])]
    context = _context([rdb, graph, merge], [_result(rdb, rdb_records), _result(graph, graph_records)])
    records = asyncio.run(InternalTransformExecutor().execute(merge, context))
    assert len(records) == 3 and {record.entity_id for record in records} == {"etf:beta"}
    assert {record.payload["field"] for record in records} == {
        "product.name", "product.aum", "graph.holds",
    }
    assert records[-1].metadata["path_provenance"] == [{"canonical_fact_id": "fact:beta"}]
    assert records[-1].source_id == graph_records[0].source_id
    normalized = QueryExecutor._with_provenance(records[-1], merge)
    assert normalized.source == "graph" and normalized.step_id == merge.step_id


@pytest.mark.parametrize("key,value", [("dataset_snapshot", "2026-08-23"), ("generation", "stale")])
def test_cross_store_snapshot_or_generation_mismatch_blocks_merge(key, value):
    rdb, graph = _step("rdb"), _step("graph")
    merge = _step("internal")
    merge.depends_on = ["rdb", "graph"]
    context = _context([rdb, graph, merge], [
        _result(rdb, [_record("rdb", "etf:alpha", "product.name", "Alpha")]),
        _result(graph, [_record("graph", "etf:alpha", "graph.holds", "Security", **{key: value})]),
    ])
    with pytest.raises(RetrieverUnavailableError, match="mismatch"):
        asyncio.run(InternalTransformExecutor().execute(merge, context))


def test_complete_candidate_merge_still_requires_snapshot_evidence():
    rdb, graph = _step("rdb"), _step("graph")
    merge = _step("internal", require_complete_candidates=True)
    merge.depends_on = ["rdb", "graph"]
    rdb_record = _record("rdb", "etf:alpha", "product.name", "Alpha")
    graph_record = _record("graph", "etf:alpha", "graph.holds", "Security")
    graph_record.metadata.pop("dataset_snapshot")
    context = _context([rdb, graph, merge], [
        _result(rdb, [rdb_record]), _result(graph, [graph_record]),
    ])
    with pytest.raises(RetrieverUnavailableError, match="missing"):
        asyncio.run(InternalTransformExecutor().execute(merge, context))


def _hit(number):
    return SemanticSearchHit(document=SemanticDocument(
        document_id=f"doc:{number}", entity_id=f"etf:{number}", source_dataset="PREF02N001",
        source_record_key=str(number), source_field="source.cu_strtegy", raw_text="market neutral",
        normalized_text="market neutral", product_type="ETF", dataset_snapshot=SNAPSHOT,
    ), score=1.0, rank=number + 1)


class SemanticStore:
    def __init__(self, count):
        self.count = count
        self.requested_limit = None

    def validate(self, **kwargs):
        pass

    def validate_derived_manifest(self, **kwargs):
        pass

    def bm25_search(self, query, *, top_k, **kwargs):
        self.requested_limit = top_k
        return [_hit(number) for number in range(min(top_k, self.count))]

    def vector_search(self, query, *, top_k, **kwargs):
        return self.bm25_search(query, top_k=top_k)


@pytest.mark.parametrize("count,complete", [(2, 1), (4, 0)])
def test_bm25_candidate_completeness_uses_lookahead_and_preserves_ids(count, complete):
    store = SemanticStore(count)
    retriever = RealBM25Retriever(store, SearchSettings(candidate_limit=3),
                                  snapshot_date=SNAPSHOT, canonical_v2=True)
    step = _step("bm25", query_terms=["market neutral"], require_complete_candidates=True)
    result = asyncio.run(retriever.retrieve_with_result(step, _context([step])))
    assert store.requested_limit == 4
    assert result.counts["candidate_set_complete"] == complete
    assert result.records[0].entity_id == "etf:0"
    assert result.records[0].metadata["generation"] == GENERATION
    assert (result.total_matches is not None) == bool(complete)


def test_semantic_projection_is_planned_as_canonical_candidates_then_rdb():
    query = GroundedQuery(
        parsed_query=ParsedQuery(original_question="market neutral ETF ticker", intent="search_product",
                                 semantic_terms=["market neutral"], requires_semantic_search=True),
        grounded_requested_fields=[GroundedField(raw_text="ticker", canonical_field="product.ticker",
                                                status=GroundingStatus.RESOLVED)],
        canonical_fields={"ticker": "product.ticker"},
    )
    plan = asyncio.run(DeterministicSupervisorPlanner().create_plan(query))
    semantic = next(step for step in plan.steps if step.source is RetrievalSource.BM25)
    rdb = next(step for step in plan.steps if step.source is RetrievalSource.RDB)
    assert semantic.depends_on == []
    assert semantic.inputs["require_complete_candidates"] is True
    assert rdb.depends_on == [semantic.step_id]
    assert rdb.inputs["candidate_ids_from"] == [semantic.step_id]
    assert rdb.inputs["requested_fields"] == ["product.ticker"]


class Embedding:
    dimension = 1
    model_name = "fixture"

    async def embed_query(self, query):
        return [1.0]


def test_vector_relevance_window_is_not_declared_complete_for_financial_ranking():
    retriever = RealVectorRetriever(SemanticStore(2), Embedding(), SearchSettings(),
                                    snapshot_date=SNAPSHOT)
    step = _step("vector", query_terms=["전략"])
    result = asyncio.run(retriever.retrieve_with_result(step, _context([step])))
    assert len(result.records) == 2
    assert result.counts["candidate_set_complete"] == 0
    assert result.total_matches is None


@pytest.mark.parametrize("level", [4, 5])
def test_graph_filter_metric_ranking_and_two_hop_fieldwise_comparison(level):
    fields = ["product.six_month_return", "product.aum"]
    sort = SortSpec(field="six_month_return", direction="desc")
    region = FilterSpec(field="region", operator="eq", value="미국")
    query = GroundedQuery(
        parsed_query=ParsedQuery(
            original_question="fixture operator composition",
            intent="search_product" if level == 4 else "compare_products",
            product_universe=ProductUniverseUnion(operands=["KODEX_LONG_ONLY_COMPATIBLE"]),
            requested_fields=fields, filters=[region], sort=[sort] if level == 4 else [],
            comparison=ComparisonSpec(fields=fields) if level == 5 else None,
            result_limit=ResultLimit(value=1, raw_text="상위 1개") if level == 4 else None,
        ),
        grounded_requested_fields=[GroundedField(
            raw_text=field, canonical_field=field, status=GroundingStatus.RESOLVED,
        ) for field in fields],
        grounded_sort=[GroundedSort(raw_sort=sort, canonical_field=fields[0],
                                   status=GroundingStatus.RESOLVED)] if level == 4 else [],
        grounded_filters=[GroundedFilter(
            raw_filter=region, canonical_field="product.region",
            canonical_value=CanonicalConcept.REGION_US, status=GroundingStatus.RESOLVED,
        )],
        grounded_relations=[GroundedRelation(
            raw_text="보유한", canonical_relation="holds", status=GroundingStatus.RESOLVED,
            target_value="security:fixture" if level == 4 else "organization:fixture",
            target_type="EquitySecurity" if level == 4 else "Organization",
        )],
        canonical_fields={field: field for field in [*fields, "product.region"]},
    )
    plan = asyncio.run(DeterministicSupervisorPlanner().create_plan(query))
    graph = next(step for step in plan.steps if step.source is RetrievalSource.GRAPH)
    rdb = next(step for step in plan.steps if step.source is RetrievalSource.RDB)
    merge = plan.steps[-1]
    assert not plan.unsupported_constraint_ids
    assert graph.inputs["paths"][0]["relations"] == (
        ["holds"] if level == 4 else ["holds", "securityIssuedBy"]
    )
    assert graph.inputs["limit"] > (query.parsed_query.result_limit.value if level == 4 else 1)
    assert graph.depends_on == [] and rdb.depends_on == [graph.step_id]
    compiler = GraphQueryCompiler(GraphMappingRegistry(version="canonical-v2"), snapshot=SNAPSHOT)
    compiled_graph = compiler.compile(graph, graph.inputs["paths"][0])
    assert "HOLDS" in compiled_graph.cypher
    assert ("SECURITY_ISSUED_BY" in compiled_graph.cypher) == (level == 5)
    snapshot = V2SnapshotSelection(
        date.fromisoformat(SNAPSHOT), GENERATION, "merged-optical-1.4",
        ("organizer", "holdings"), ("PREF01N001", "dataset:kodex-holdings"),
    )
    graph_result = asyncio.run(_graph(GraphBackend()).retrieve_with_result(graph, _context(plan.steps)))
    graph_records = graph_result.records
    graph_step_result = _result(graph, graph_records, **graph_result.model_dump(exclude={"records"}))
    # Actual graph records feed the dependency binder and the real RDB compiler.
    bound = _bind_dependency_candidates(rdb, _context(plan.steps, [graph_step_result]))
    compiled_rdb = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100,
    ).compile(bound, snapshot)
    assert compiled_rdb.ranking_applied == (level == 4)
    if level == 5:
        assert {contract["canonical_field"] for contract in rdb.inputs["comparison_contracts"]} == set(fields)
    # The higher-valued product is deliberately second in graph traversal
    # order; only the RDB's verified order can determine the final Top-K.
    rdb_records = [_record("rdb", entity, field, value)
                   for entity, value in (("etf:beta", "20"), ("etf:alpha", "10"))
                   for field in fields]
    metadata = {"rankable_total": 2, "returned_count": 2,
                "ranked_candidate_ids": ["etf:beta", "etf:alpha"]}
    context = _context(plan.steps, [
        graph_step_result, _result(rdb, rdb_records, **metadata),
    ])
    records = asyncio.run(InternalTransformExecutor().execute(merge, context))
    selected = {"etf:beta"} if level == 4 else {"etf:alpha", "etf:beta"}
    assert {record.entity_id for record in records} == selected
    assert len(records) == 3 * len(selected)
    for entity in selected:
        assert set(fields).issubset({record.payload["field"] for record in records if record.entity_id == entity})
