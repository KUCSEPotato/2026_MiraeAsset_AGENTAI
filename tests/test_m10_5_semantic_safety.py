import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import insert

from app.agent.service import create_production_answer_service
from app.data.schema import canonical_products
from app.domain.models import (
    AnswerabilityReasonCode,
    BooleanExpression,
    BooleanNodeType,
    FilterOperator,
    FilterSpec,
    ExecutionContext,
    ExecutionResult,
    RetrievalRecord,
    SemanticCoverageStatus,
)
from app.entity.lookup import StaticEntityLookup
from app.entity.rdb_lookup import RDBEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.ontology.loader import OntologyLoader
from app.ontology.canonical_fields import ONTOLOGY_CANONICAL_FIELDS
from app.ontology.rdf_service import RDFOntologyService
from app.planning.coordinator import QueryPlanner
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.retrieval.rdb import (
    RDBFieldRegistry,
    RDBQueryCompiler,
    RealRDBRetriever,
)
from tests.data_helpers import postgres_engine
from tests.test_rdb_retriever import product
from tests.evidence_helpers import make_bundle, make_evidence, make_query, validate


def _ontology() -> RDFOntologyService:
    return RDFOntologyService(
        OntologyLoader(
            Path("ontology"),
            known_canonical_fields=ONTOLOGY_CANONICAL_FIELDS,
        ).load()
    )


def _planner() -> QueryPlanner:
    metadata = RoutingMetadataRegistry()
    return QueryPlanner(
        routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata),
    )


async def _semantic_plan(question: str):
    analyzer = RuleBasedQueryAnalyzer()
    parsed = await analyzer.analyze(question)
    resolved = await RegistryEntityResolver(StaticEntityLookup()).resolve(parsed)
    grounded = await _ontology().ground(resolved)
    return parsed, grounded, await _planner().create_plan(grounded)


def _run_plan(question: str):
    return asyncio.run(_semantic_plan(question))


def _assert_blocked(question: str) -> None:
    with pytest.raises(UnsupportedQuerySemanticsError):
        _run_plan(question)


def test_material_clause_tracking_is_stable_and_fail_closed() -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("반도체 ETF"))

    assert [item.constraint_id for item in parsed.semantic_constraints] == [
        "C1",
        "C2",
        "C3",
    ]
    assert parsed.semantic_coverage is SemanticCoverageStatus.INCOMPLETE
    assert [item.raw_text for item in parsed.unparsed_material_spans] == ["반도체"]
    assert parsed.unsupported_constraint_ids == ["C1"]


def test_filter_operator_value_contract_and_boolean_tree() -> None:
    item = FilterSpec(field="region", operator="in", value=["미국", "일본"])
    assert item.operator is FilterOperator.IN
    with pytest.raises(ValueError, match="non-empty collection"):
        FilterSpec(field="region", operator="in", value="미국")
    with pytest.raises(ValueError, match="exactly two"):
        FilterSpec(field="aum", operator="between", value=[1])

    expression = BooleanExpression(
        node_type=BooleanNodeType.OR,
        children=[
            BooleanExpression(node_type="predicate", constraint_id="C1"),
            BooleanExpression(
                node_type="not",
                children=[
                    BooleanExpression(node_type="predicate", constraint_id="C2")
                ],
            ),
        ],
    )
    assert expression.node_type is BooleanNodeType.OR


def test_typed_numeric_constraints_preserve_normalized_value_then_block() -> None:
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("총보수 0.1% 이하 ETF")
    )
    value = parsed.filters[0].value
    assert value.raw == "0.1%"
    assert value.normalized == pytest.approx(0.001)
    assert value.unit.value == "ratio"
    _assert_blocked("총보수 0.1% 이하 ETF")


@pytest.mark.parametrize(
    "question",
    [
        "반도체 ETF",
        "순자산 1,000억 이상 ETF",
        "총보수 0.1% 이하 ETF",
        "미국 ETF 또는 국내 채권",
        "상위 10개 ETF",
        "ETF와 ETN 총보수 비교",
        "안전한 ETF 추천",
        "미국 ETF는 몇 개인가?",
        "2024년 말 기준 ETF",
        "AI와 친환경 산업에 모두 투자하는 ETF",
    ],
)
def test_unsupported_audit_cases_are_explicitly_blocked(question: str) -> None:
    _assert_blocked(question)


def test_supported_audit_cases_preserve_executable_constraints() -> None:
    _, _, negated = _run_plan("미국 제외 ETF")
    assert negated.steps[0].inputs["filters"][0]["raw"]["operator"] == "ne"

    _, _, region_or = _run_plan("미국 또는 일본 ETF")
    assert region_or.steps[0].inputs["filters"][0]["canonical_value"] == [
        "Region.US",
        "Region.JP",
    ]

    _, _, limited = _run_plan("ETF 10개만")
    assert limited.steps[0].inputs["limit"] == 10

    _, _, projected = _run_plan("가격과 NAV를 알려줘")
    assert projected.steps[0].inputs["requested_fields"] == [
        "product.price",
        "product.nav",
    ]

    # M10.9-C1 requires one source-scoped comparison contract per ranking
    # metric.  This broad AUM + unknown-scale expense request must now fail
    # before retrieval rather than merely carrying an unsafe sort downstream.
    _assert_blocked("순자산이 크고 총보수가 낮은 ETF")


def test_negation_or_and_limit_execute_with_sql_null_policy(tmp_path: Path) -> None:
    engine = postgres_engine(tmp_path / "semantic-safety")
    rows = [
        product("US", region="Region.US"),
        product("JP", region="Region.JP"),
        product("KR", region="Region.KR"),
        product("NULL", region=None),
    ]
    rows.extend(product(f"EXTRA-{index}", region="Region.JP") for index in range(12))
    with engine.begin() as connection:
        connection.execute(insert(canonical_products), rows)
    retriever = RealRDBRetriever(
        engine,
        RDBQueryCompiler(
            RDBFieldRegistry(),
            default_limit=100,
            max_limit=100,
            snapshot_date="2026-07-11",
        ),
    )

    async def ids(question: str) -> list[str]:
        _, _, plan = await _semantic_plan(question)
        records = await retriever.retrieve(
            plan.steps[0], ExecutionContext(plan=plan)
        )
        return [item.entity_id for item in records if item.entity_id is not None]

    try:
        excluded = asyncio.run(ids("미국 제외 ETF"))
        selected = asyncio.run(ids("미국 또는 일본 ETF"))
        limited = asyncio.run(ids("ETF 10개만"))
    finally:
        engine.dispose()

    assert "US" not in excluded
    assert "NULL" not in excluded  # SQL UNKNOWN is conservatively excluded.
    assert "KR" not in selected and "NULL" not in selected
    assert {"US", "JP"}.issubset(selected)
    assert len(limited) == 10


@pytest.mark.parametrize(
    ("question", "relation", "target"),
    [
        ("발행사가 대한민국인 채권", "issuedBy", "대한민국"),
        ("기초지수가 S&P 500인 ETF", "tracks", "S&P 500"),
        ("표시통화가 USD인 ETF", "denominatedIn", "USD"),
        ("위험등급 1등급인 ETF", "hasRiskGrade", "1"),
    ],
)
def test_relation_targets_reach_parameterized_graph_plan(
    question: str,
    relation: str,
    target: str,
) -> None:
    _, _, plan = _run_plan(question)
    graph_step = next(step for step in plan.steps if step.source.value == "graph")
    path = graph_step.inputs["paths"][0]
    assert path["relations"] == [relation]
    assert path["target_values"] == [target]


@pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="disposable PostgreSQL is unavailable",
)
def test_relation_chain_is_one_bound_two_hop_path(tmp_path: Path) -> None:
    engine = postgres_engine(tmp_path / "relation-chain")
    manager_product = product("SAMSUNG-MANAGED-ETF")
    manager_product.update(
        {
            "asset_manager": "삼성",
            "source_dataset": "domestic_etf",
        }
    )
    with engine.begin() as connection:
        connection.execute(insert(canonical_products), manager_product)

    async def create():
        parsed = await RuleBasedQueryAnalyzer().analyze(
            "삼성이 운용하는 ETF의 기초지수"
        )
        resolved = await RegistryEntityResolver(
            RDBEntityLookup(engine, snapshot_date="2026-07-11")
        ).resolve(parsed)
        grounded = await _ontology().ground(resolved)
        return await _planner().create_plan(grounded)

    try:
        plan = asyncio.run(create())
    finally:
        engine.dispose()
    graph_step = next(step for step in plan.steps if step.source.value == "graph")
    assert graph_step.inputs["paths"] == [
        {
            "relations": ["managedBy", "tracks"],
            "directions": ["incoming", "outgoing"],
            "raw_relations": ["운용하는", "기초지수"],
            "target_values": [None, None],
            "target_types": [None, None],
            "constraint_ids": ["C3", "C5"],
        }
    ]


def test_no_omission_validator_rejects_removed_constraint_coverage() -> None:
    _, grounded, plan = _run_plan("미국 제외 ETF")
    unsafe = plan.model_copy(deep=True)
    unsafe.steps[0].covers_constraint_ids.remove("C1")

    with pytest.raises(
        UnsupportedQuerySemanticsError,
        match="unsupported query semantics",
    ) as caught:
        StructuredQueryPlanValidator(RoutingMetadataRegistry()).validate(
            unsafe, grounded
        )
    assert "uncovered_constraint:C1" in caught.value.reasons


def test_hybrid_ranking_is_blocked_before_execution() -> None:
    _assert_blocked("AI 관련 ETF 중 순자산이 큰 상품을 찾아줘")


def test_runtime_ranking_verification_blocks_false_application_claim() -> None:
    query = make_query(sort_fields=["product.aum"])
    record = RetrievalRecord(
        source="internal",
        source_id="rank:P1",
        entity_id="P1",
        payload={"field": "product.aum", "value": 100},
        metadata={
            "real_rdb": True,
            "transform_operation": "rank_candidates",
            "ranking_applied": False,
        },
    )
    result = validate(
        query,
        make_bundle(
            [
                make_evidence(
                    field="product.aum",
                    value="100",
                    metadata=record.metadata,
                )
            ],
            ExecutionResult(records=[record]),
        ),
    )

    assert AnswerabilityReasonCode.RANKING_NOT_APPLIED in result.reason_codes
    assert result.answerable is False


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="production service requires a configured PostgreSQL URL",
)
def test_public_answer_contract_returns_safe_semantic_response_without_execution() -> None:
    class MustNotExecute:
        async def execute(self, plan):
            del plan
            raise AssertionError("unsafe plan reached execution")

    service = create_production_answer_service(executor=MustNotExecute())
    result = asyncio.run(service.answer("반도체 ETF"))
    trace = json.loads(result.think_trace)
    assert trace["status"] == "unsupported"
    assert "조건을 모두 정확하게 해석" in result.answer

    comparison = asyncio.run(service.answer("순자산이 큰 ETF 3개"))
    context = json.loads(comparison.retrieved_context)
    assert context["validation"]["reasons"] == [
        "unsupported_comparison:aum_scope_spans_or_cannot_exclude_incompatible_sources"
    ]
