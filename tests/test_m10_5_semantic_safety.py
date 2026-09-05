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
    CanonicalEntity,
    EntityMention,
    SemanticCoverageStatus,
)
from app.entity.lookup import StaticEntityLookup
from app.entity.rdb_lookup import RDBEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.entity.normalization import (
    entity_lookup_keys,
    normalized_entity_form,
)
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
from app.retrieval.rdb_v2 import CanonicalV2FieldRegistry
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


_TIGER_SP500 = CanonicalEntity(
    canonical_id="etf_kr:KR7360750004",
    entity_type="product",
    official_name=(
        "미래에셋 TIGER 미국S&P500증권상장지수투자신탁(주식)"
    ),
    aliases=["TIGER 미국S&P500"],
    identifiers={"isin": "KR7360750004"},
)


async def _tiger_plan(question: str):
    parsed = await RuleBasedQueryAnalyzer().analyze(question)
    resolved = await RegistryEntityResolver(
        StaticEntityLookup([_TIGER_SP500])
    ).resolve(parsed)
    ontology = RDFOntologyService(
        OntologyLoader(
            Path("ontology"),
            known_canonical_fields=CanonicalV2FieldRegistry().canonical_fields,
            version="team-v1",
        ).load()
    )
    grounded = await ontology.ground(resolved)
    return parsed, resolved, grounded, await _planner().create_plan(grounded)


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


def test_named_product_facets_do_not_pollute_entity_identity() -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(
        "TIGER 미국S&P500 ETF의 수익률과 위험 정보를 알려줘"
    ))

    assert [(item.raw_text, item.entity_type) for item in parsed.entities] == [
        ("TIGER 미국S&P500", "product")
    ]
    assert parsed.product_types == ["ETF"]
    assert parsed.requested_fields == ["수익률", "위험 정보"]
    assert parsed.requires_semantic_search is False
    assert parsed.semantic_terms == []


@pytest.mark.parametrize(
    "question",
    [
        "TIGER 미국S&P500 ETF 알려줘",
        "TIGER 미국S&P500 ETF 정보 알려줘",
        "TIGER 미국S&P500 ETF에 대해 설명해줘",
    ],
)
def test_named_product_information_uses_basic_canonical_projection(
    question: str,
) -> None:
    parsed, resolved, _, plan = asyncio.run(_tiger_plan(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.requires_semantic_search is False
    assert parsed.unparsed_material_spans == []
    assert resolved.resolved_entities[0].canonical_id == _TIGER_SP500.canonical_id
    inputs = plan.steps[0].inputs
    assert inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert inputs["projection_profile"] == "BASIC_PRODUCT"
    assert inputs["requested_fields"] == [
        "product.name",
        "product.product_type",
        "product.ticker",
        "product.isin",
    ]


def test_named_product_generic_return_uses_default_period_without_ranking() -> None:
    _, resolved, grounded, plan = asyncio.run(
        _tiger_plan("TIGER 미국S&P500 ETF의 수익률 알려줘")
    )

    assert resolved.resolved_entities[0].canonical_id == _TIGER_SP500.canonical_id
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.one_year_return"
    ]
    inputs = plan.steps[0].inputs
    assert inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert inputs["requested_fields"] == ["product.one_year_return"]
    assert inputs["sort"] == []
    assert inputs["sort_operations"] == []
    assert inputs["top_n"] is None
    resolution = inputs["comparison_contracts"][0]["metric_resolution"]
    assert resolution["period"] == "1Y"
    assert resolution["period_source"] == "DEFAULT_POLICY"
    assert "기간이 별도로 지정되지 않아" in resolution["disclosure"]


def test_named_product_explicit_return_period_does_not_rank() -> None:
    parsed, _, grounded, plan = asyncio.run(
        _tiger_plan("TIGER 미국S&P500 ETF의 최근 6개월 수익률 알려줘")
    )

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.unparsed_material_spans == []
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.six_month_return"
    ]
    inputs = plan.steps[0].inputs
    assert inputs["requested_fields"] == ["product.six_month_return"]
    assert inputs["sort"] == []
    assert inputs["top_n"] is None
    resolution = inputs["comparison_contracts"][0]["metric_resolution"]
    assert resolution["period"] == "6M"
    assert resolution["period_source"] == "EXPLICIT_QUERY"


@pytest.mark.parametrize("risk_phrase", ["위험 정보", "위험", "위험도", "리스크"])
def test_named_product_risk_aliases_project_only_canonical_risk_grade(
    risk_phrase: str,
) -> None:
    _, _, grounded, plan = asyncio.run(
        _tiger_plan(f"TIGER 미국S&P500 ETF의 {risk_phrase} 알려줘")
    )

    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.risk_grade"
    ]
    assert plan.steps[0].inputs["requested_fields"] == ["product.risk_grade"]


def test_missing_named_product_risk_grade_remains_explicitly_unavailable() -> None:
    _, _, grounded, _ = asyncio.run(
        _tiger_plan("TIGER 미국S&P500 ETF의 위험 정보 알려줘")
    )
    name_evidence = make_evidence(
        field="product.name",
        value=_TIGER_SP500.official_name,
    ).model_copy(update={"entity_id": _TIGER_SP500.canonical_id})

    result = validate(grounded, make_bundle([name_evidence]))

    assert not result.answerable
    assert AnswerabilityReasonCode.MISSING_REQUIRED_FIELD in result.reason_codes
    assert result.missing_fields == ["product.risk_grade"]


def test_named_product_return_and_risk_reuse_one_entity_resolution() -> None:
    parsed, resolved, grounded, plan = asyncio.run(
        _tiger_plan(
            "TIGER 미국S&P500 ETF의 수익률과 위험 정보를 알려줘"
        )
    )

    assert len(resolved.resolved_entities) == 1
    assert resolved.resolved_entities[0].canonical_id == _TIGER_SP500.canonical_id
    assert parsed.requires_semantic_search is False
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.one_year_return",
        "product.risk_grade",
    ]
    inputs = plan.steps[0].inputs
    assert inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert inputs["requested_fields"] == [
        "product.one_year_return",
        "product.risk_grade",
    ]
    assert inputs["comparison_contracts"][0]["metric_resolution"][
        "period_source"
    ] == "DEFAULT_POLICY"


def test_direct_named_product_and_index_targets_have_clean_entity_spans() -> None:
    product = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("TIGER 미국S&P500 ETF를 알려줘")
    )
    index = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("S&P 500을 추종하는 ETF를 알려줘")
    )

    assert [(item.raw_text, item.entity_type) for item in product.entities] == [
        ("TIGER 미국S&P500", "product")
    ]
    assert product.product_types == ["ETF"]
    assert [(item.raw_text, item.entity_type) for item in index.entities] == [
        ("S&P 500", "index")
    ]


def test_generic_entity_normalization_handles_spacing_and_context_suffixes() -> None:
    assert normalized_entity_form(
        "TIGER 미국 S&P500 ETF", "product"
    ) == normalized_entity_form("TIGER 미국S&P500", "product")
    assert normalized_entity_form(
        "미래에셋자산운용사", "management_company"
    ) == normalized_entity_form("미래에셋자산운용", "management_company")
    assert normalized_entity_form(
        "㈜ 예시기업 Co., Ltd.", "company"
    ) == normalized_entity_form("예시기업", "company")
    assert normalized_entity_form("S&P500", "index") == normalized_entity_form(
        "S&P 500", "index"
    )
    assert "tiger미국s&p500" in entity_lookup_keys(
        "TIGER 미국 S&P500 ETF", "product"
    )


def test_normalized_entity_resolution_is_exact_and_ambiguity_safe() -> None:
    entities = [
        CanonicalEntity(
            canonical_id="ETF:1",
            entity_type="product",
            official_name="TIGER 미국S&P500",
            aliases=["미국 S&P 500 대표 ETF"],
        ),
        CanonicalEntity(
            canonical_id="ORG:1",
            entity_type="management_company",
            official_name="미래에셋자산운용",
        ),
        CanonicalEntity(
            canonical_id="INDEX:1",
            entity_type="index",
            official_name="S&P 500",
        ),
    ]

    async def resolve(raw: str, entity_type: str):
        parsed = await RuleBasedQueryAnalyzer().analyze("ETF를 알려줘")
        parsed = parsed.model_copy(update={
            "entities": [EntityMention(raw_text=raw, entity_type=entity_type)]
        })
        return (
            await RegistryEntityResolver(StaticEntityLookup(entities)).resolve(parsed)
        ).resolved_entities[0]

    product = asyncio.run(resolve("TIGER 미국 S&P500 ETF", "product"))
    manager = asyncio.run(resolve("미래에셋자산운용사", "management_company"))
    index = asyncio.run(resolve("S&P500", "index"))

    assert product.canonical_id == "ETF:1"
    assert product.resolution_method == "NORMALIZED_EXACT"
    assert manager.canonical_id == "ORG:1"
    assert index.canonical_id == "INDEX:1"
    assert all(item.confidence == 1.0 for item in (product, manager, index))


def test_fuzzy_entity_resolution_requires_a_unique_high_confidence_winner() -> None:
    entities = [
        CanonicalEntity(
            canonical_id="ETF:TIGER-SP500",
            entity_type="product",
            official_name="TIGER 미국S&P500",
        ),
        CanonicalEntity(
            canonical_id="ETF:OTHER",
            entity_type="product",
            official_name="다른 글로벌채권 ETF",
        ),
    ]
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("ETF를 알려줘"))
    parsed = parsed.model_copy(update={
        "entities": [EntityMention(
            raw_text="TIGER 미국S&P50O",
            entity_type="product",
        )]
    })

    resolved = asyncio.run(
        RegistryEntityResolver(StaticEntityLookup(entities)).resolve(parsed)
    ).resolved_entities[0]

    assert resolved.canonical_id == "ETF:TIGER-SP500"
    assert resolved.resolution_method == "FUZZY_MATCH"
    assert resolved.candidate_diagnostics[0].match_score >= 0.9
    assert resolved.candidate_diagnostics[0].rejection_reason is None


def test_exact_canonical_name_precedes_a_conflicting_alias() -> None:
    entities = [
        CanonicalEntity(
            canonical_id="ETF:CANONICAL",
            entity_type="product",
            official_name="알파 대표 ETF",
        ),
        CanonicalEntity(
            canonical_id="ETF:ALIAS",
            entity_type="product",
            official_name="베타 대표 ETF",
            aliases=["알파 대표 ETF"],
        ),
    ]
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("ETF를 알려줘"))
    parsed = parsed.model_copy(update={
        "entities": [EntityMention(raw_text="알파 대표 ETF", entity_type="product")]
    })

    resolved = asyncio.run(
        RegistryEntityResolver(StaticEntityLookup(entities)).resolve(parsed)
    ).resolved_entities[0]

    assert resolved.canonical_id == "ETF:CANONICAL"
    assert resolved.resolution_method == "EXACT_CANONICAL"


def test_empty_entity_span_is_a_parse_failure_not_entity_not_found() -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("ETF를 알려줘"))
    parsed = parsed.model_copy(update={
        "entities": [EntityMention(raw_text="   ", entity_type="product")]
    })

    resolved = asyncio.run(
        RegistryEntityResolver(StaticEntityLookup()).resolve(parsed)
    ).resolved_entities[0]

    assert resolved.resolution_status.value == "unresolved"
    assert resolved.resolution_reason == "ENTITY_PARSE_FAILED"


def test_fuzzy_entity_resolution_does_not_choose_close_competing_candidates() -> None:
    entities = [
        CanonicalEntity(
            canonical_id="ETF:ALPHA",
            entity_type="product",
            official_name="알파 글로벌 혁신성장 A",
        ),
        CanonicalEntity(
            canonical_id="ETN:ALPHA",
            entity_type="product",
            official_name="알파 글로벌 혁신성장 B",
        ),
    ]
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("ETF를 알려줘"))
    parsed = parsed.model_copy(update={
        "entities": [EntityMention(
            raw_text="알파 글로벌 혁신성장 C",
            entity_type="product",
        )]
    })

    resolved = asyncio.run(
        RegistryEntityResolver(StaticEntityLookup(entities)).resolve(parsed)
    ).resolved_entities[0]

    assert resolved.resolution_status.value == "ambiguous"
    assert resolved.canonical_id is None
    assert set(resolved.candidate_ids) == {"ETF:ALPHA", "ETN:ALPHA"}
    assert all(
        item.rejection_reason == "AMBIGUOUS"
        for item in resolved.candidate_diagnostics
    )


def test_below_threshold_candidate_remains_explicitly_unresolved() -> None:
    entities = [CanonicalEntity(
        canonical_id="ETF:SP500",
        entity_type="product",
        official_name="TIGER 미국S&P500",
    )]
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze("ETF를 알려줘"))
    parsed = parsed.model_copy(update={
        "entities": [EntityMention(
            raw_text="TIGER 미국나스닥100",
            entity_type="product",
        )]
    })

    resolved = asyncio.run(
        RegistryEntityResolver(StaticEntityLookup(entities)).resolve(parsed)
    ).resolved_entities[0]

    assert resolved.resolution_status.value == "unresolved"
    assert resolved.resolution_reason == "ENTITY_UNRESOLVED"
    assert resolved.candidate_ids == []
    assert resolved.candidate_diagnostics[0].rejection_reason == "BELOW_THRESHOLD"


def test_holding_phrase_extracts_generic_organization_relation() -> None:
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("삼성전자를 편입한 ETF를 알려줘")
    )

    assert [(item.raw_text, item.entity_type) for item in parsed.entities] == [
        ("삼성전자", "organization")
    ]
    assert parsed.relations[0].raw_text == "편입한"
    assert parsed.relations[0].target_type == "Organization"
    assert parsed.unparsed_material_spans == []


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
        ("기초지수가 S&P 500인 ETF", "tracks", "TEST_INDEX_SP500"),
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
