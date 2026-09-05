"""Composition contracts at the parser → resolver → ontology → planner boundary.

All identities come from the existing static/graph fixtures. These tests never
connect to a database, model service, graph server, or production artifact.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.models import (
    BooleanExpression, CanonicalEntity, FilterSpec, GroundedQuery, MetricSpec, ParsedQuery,
    RelationMention, SemanticConstraint, SourceSpan, TemporalSpec,
)
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.planning.coordinator import QueryPlanner
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.semantic_ir import SemanticQueryIR, build_semantic_ir
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.normalization import normalize_query_semantics
from app.retrieval.rdb_v2 import CanonicalV2FieldRegistry
from tests.test_graph_retriever import StubGraphBackend
from tests.test_m10_5_semantic_safety import _TIGER_SP500


@pytest.fixture(scope="module")
def products() -> tuple[CanonicalEntity, ...]:
    # Reuse the graph fixture's domestic ETP identity/name and the canonical
    # entity fixture used by upstream entity-centric lookup regression tests.
    graph_fixture = asyncio.run(StubGraphBackend().query("fixture", {}))[0]["nodes"][0]
    return (
        CanonicalEntity(canonical_id=graph_fixture["entity_id"], entity_type="product",
                        official_name=graph_fixture["display_name"]),
        _TIGER_SP500,
    )


@pytest.fixture(scope="module")
def ontology() -> RDFOntologyService:
    return RDFOntologyService(OntologyLoader(
        Path("ontology"), version="team-v1",
        known_canonical_fields=CanonicalV2FieldRegistry().canonical_fields,
    ).load())


def _planner() -> QueryPlanner:
    metadata = RoutingMetadataRegistry()
    return QueryPlanner(
        routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata),
    )


async def _ground(question_or_query, products, ontology):
    parsed = (await RuleBasedQueryAnalyzer().analyze(question_or_query)
              if isinstance(question_or_query, str)
              else normalize_query_semantics(question_or_query))
    resolved = await RegistryEntityResolver(StaticEntityLookup(list(products))).resolve(parsed)
    grounded = await ontology.ground(resolved)
    return parsed, grounded


async def _plan(question_or_query, products, ontology):
    parsed, grounded = await _ground(question_or_query, products, ontology)
    return parsed, grounded, await _planner().create_plan(grounded)


def _operators(plan):
    ir = SemanticQueryIR.model_validate(plan.semantic_ir)
    return {operator.kind.value for operator in ir.operators}


def test_level1_entity_field_lookup_carries_one_resolved_identity(products, ontology) -> None:
    parsed, grounded, plan = asyncio.run(_plan(
        f"{products[0].official_name}의 AUM을 알려줘", products, ontology,
    ))
    assert parsed.semantic_coverage == "complete"
    assert [item.canonical_id for item in grounded.resolved_entities] == [products[0].canonical_id]
    assert len(plan.steps) == 1 and plan.steps[0].source == "rdb"
    assert plan.steps[0].inputs["entity_ids"] == [products[0].canonical_id]
    assert plan.steps[0].inputs["requested_fields"] == ["product.aum"]
    assert {"ResolveEntity", "ResolveMetric", "ProjectField"} <= _operators(plan)


def test_level2_filter_return_sort_bottomk_has_one_global_order(products, ontology) -> None:
    parsed, _, plan = asyncio.run(_plan(
        "국내 ETF 중 미국 주식형 6개월 수익률 하위 3개", products, ontology,
    ))
    assert parsed.semantic_coverage == "complete"
    assert parsed.sort[0].direction == "asc"
    step = plan.steps[0]
    assert step.source == "rdb"
    assert {item["canonical_field"] for item in step.inputs["filters"]} == {
        "product.region", "product.asset_type",
    }
    assert step.inputs["sort_operations"] == [{
        "semantic_metric_key": "product.six_month_return", "direction": "asc",
    }]
    assert step.inputs["top_n"] == {"value": 3}
    assert {"Filter", "TemporalResolve", "ResolveMetric", "Sort", "TopK"} <= _operators(plan)


def test_level3_multiple_entities_multiple_fields_share_approved_contracts(products, ontology) -> None:
    names = [products[0].official_name, products[1].aliases[0]]
    parsed, grounded, plan = asyncio.run(_plan(
        f"{names[0]}과 {names[1]}의 수익률과 AUM을 비교해줘", products, ontology,
    ))
    assert parsed.semantic_coverage == "complete"
    assert len(grounded.resolved_entities) == 2
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert set(step.inputs["entity_ids"]) == {item.canonical_id for item in products}
    assert step.inputs["comparison"] == {
        "mode": "fieldwise", "fields": ["product.one_year_return", "product.aum"],
    }
    contracts = {item["canonical_field"]: item for item in step.inputs["comparison_contracts"]}
    assert set(contracts) == {"product.one_year_return", "product.aum"}
    assert {item["dataset"] for item in contracts.values()} == {"PREF01N001"}
    assert contracts["product.one_year_return"]["metric_resolution"]["period_source"] == "DEFAULT_POLICY"
    assert {"ResolveEntity", "ProjectField", "Compare", "ResolveMetric", "TemporalResolve"} <= _operators(plan)


@pytest.mark.parametrize("phrase,period", [
    ("1D 수익률", "1D"), ("1M 수익률", "1M"), ("3M 수익률", "3M"),
    ("6M 수익률", "6M"), ("1Y 수익률", "1Y"), ("YTD 수익률", "YTD"),
    ("수익률", "1Y"),
])
def test_return_period_operator_provenance_matches_execution_contract(phrase, period, products, ontology) -> None:
    _, _, plan = asyncio.run(_plan(f"국내 ETF 중 {phrase} 상위 3개", products, ontology))
    temporal = next(item for item in plan.semantic_ir["operators"] if item["kind"] == "TemporalResolve")
    contract = plan.steps[0].inputs["comparison_contracts"][0]
    assert temporal["parameters"]["period"] == contract["exact_period"] == period
    assert temporal["parameters"]["period_source"] == (
        "DEFAULT_POLICY" if phrase == "수익률" else "EXPLICIT_QUERY"
    )


@pytest.mark.parametrize("query,reason", [
    (ParsedQuery(original_question="ETF field", intent="lookup_product",
                 requested_fields=["product.unknown_metric"]), "unresolved_structured_field"),
    (ParsedQuery(original_question="ETF AUM", intent="lookup_product", requested_fields=["AUM"],
                 metrics=[MetricSpec(metric="AUM", canonical_field="product.unknown_metric")]),
     "ungrounded_metric_field:product.unknown_metric"),
    (ParsedQuery(original_question="ETF AUM 가격", intent="lookup_product", requested_fields=["AUM", "가격"],
                 metrics=[MetricSpec(metric="AUM", canonical_field="product.price")]),
     "metric_field_binding_invalid"),
    (ParsedQuery(original_question="ETF AUM", intent="lookup_product", requested_fields=["AUM"],
                 metrics=[MetricSpec(metric="RETURN", canonical_field="product.aum", temporal=TemporalSpec(period="1Y"))]),
     "metric_period_binding_invalid"),
    (ParsedQuery(original_question="unknown relation", intent="search_product",
                 relations=[RelationMention(raw_text="unknown_relation")]), "unresolved_relation"),
    ("최근 6개월 동안 AUM이 가장 많이 증가한 ETF", "historical_series_unavailable"),
    ("운용사별 ETF 개수", "group_by_execution_unsupported"),
])
def test_capability_gate_rejects_unbacked_operators(query, reason, products, ontology) -> None:
    with pytest.raises(UnsupportedQuerySemanticsError) as caught:
        asyncio.run(_plan(query, products, ontology))
    assert reason in caught.value.reasons


def test_comparison_cannot_enable_unverified_expense_ratio(products, ontology) -> None:
    question = f"{products[0].official_name}과 {products[1].aliases[0]}의 운용보수와 AUM을 비교해줘"
    with pytest.raises(UnsupportedQuerySemanticsError) as caught:
        asyncio.run(_plan(question, products, ontology))
    assert "unsupported_comparison:expense_ratio_scale_unverified" in caught.value.reasons


def test_cross_store_or_cannot_drop_semantic_branch(products, ontology) -> None:
    parsed = ParsedQuery(
        original_question="미국 또는 혁신 전략 ETF", intent="search_product",
        filters=[FilterSpec(field="region", operator="eq", value="미국", constraint_id="C1")],
        semantic_terms=["혁신 전략"], requires_semantic_search=True,
        semantic_constraints=[
            SemanticConstraint(constraint_id="C1", source_span=SourceSpan(start=0, end=2),
                               raw_text="미국", semantic_type="filter"),
            SemanticConstraint(constraint_id="C2", source_span=SourceSpan(start=6, end=11),
                               raw_text="혁신 전략", semantic_type="semantic"),
        ],
        boolean_expression=BooleanExpression(node_type="or", children=[
            BooleanExpression(node_type="predicate", constraint_id=value) for value in ("C1", "C2")
        ]),
    )
    _, grounded = asyncio.run(_ground(parsed, products, ontology))
    ir = build_semantic_ir(grounded)
    assert {item.kind.value for item in ir.operators} >= {"Filter", "SemanticSearch"}
    assert {item.constraint_id for item in grounded.semantic_constraints} == {"C1", "C2"}
    with pytest.raises(UnsupportedQuerySemanticsError) as caught:
        asyncio.run(_planner().create_plan(grounded))
    assert "cross_store_boolean_unsupported" in caught.value.reasons


@pytest.mark.parametrize("region,concept", [
    ("미국", "Region.US"), ("일본", "Region.JP"),
    ("중국", "Region.CN"), ("국내", "Region.KR"),
])
@pytest.mark.parametrize("field,canonical", [
    ("6개월 수익률", "product.six_month_return"), ("AUM", "product.aum"),
])
def test_product_scope_and_exposure_are_independent_filter_operators(
    region, concept, field, canonical, products, ontology,
) -> None:
    question = f"국내 ETF 중 {region} 주식형 {field} 하위 3개"
    parsed, grounded, plan = asyncio.run(_plan(question, products, ontology))
    assert parsed.semantic_coverage == "complete"
    assert parsed.product_universe.operands == ["DomesticETF"]
    exposure = next(item for item in grounded.grounded_filters if item.canonical_field == "product.region")
    assert exposure.canonical_value.value == concept
    constraint = next(item for item in grounded.semantic_constraints
                      if item.constraint_id == exposure.raw_filter.constraint_id)
    assert constraint.source_span.start == question.index(region, len("국내 ETF"))
    assert plan.steps[0].inputs["sort"][0]["canonical_field"] == canonical
    assert plan.steps[0].inputs["comparison_contracts"][0]["dataset"] == "PREF01N001"


def test_relation_only_or_cannot_be_lowered_to_and():
    from app.planning.predicates import structured_predicate
    constraints = [SemanticConstraint(
        constraint_id=value, source_span=SourceSpan(start=index, end=index + 1),
        raw_text=value, semantic_type="relation", status="grounded",
    ) for index, value in enumerate(("C1", "C2"))]
    query = GroundedQuery(parsed_query=ParsedQuery(
        original_question="relation alternatives", intent="search_product",
        boolean_expression=BooleanExpression(node_type="or", children=[
            BooleanExpression(node_type="predicate", constraint_id=value)
            for value in ("C1", "C2")
        ]),
    ), semantic_constraints=constraints)
    with pytest.raises(UnsupportedQuerySemanticsError, match="unsupported query semantics"):
        structured_predicate(query)


def test_unordered_result_window_is_not_mislabeled_as_topk():
    from app.domain.models import ResultLimit
    query = GroundedQuery(parsed_query=ParsedQuery(
        original_question="ETF 3개", intent="search_product",
        result_limit=ResultLimit(value=3, raw_text="3개"),
    ))
    kinds = {operator.kind.value for operator in build_semantic_ir(query).operators}
    assert "Limit" in kinds and "TopK" not in kinds
