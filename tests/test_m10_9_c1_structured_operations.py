from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.data.metric_capabilities import (
    EVALUATION_DATA_CUTOFF,
    MetricCapabilityRegistry,
    PREF01_AUM,
    PREF01_EXPENSE,
    PREF02_AUM,
    PREF02_EXPENSE,
    PRBD_CREDIT_RATING,
    PRBD_PURCHASABLE_BOND,
    PREF01_ONE_YEAR_RETURN,
    PREF02_ONE_YEAR_RETURN,
    ISHARES_SCOPED_ONE_YEAR_RETURN,
    PRFD_SHARE_CLASS_ONE_YEAR_RETURN,
    CROSS_PRODUCT_RETURN_CONTRACTS,
)
from app.domain.models import (
    ExecutionContext,
    QueryPlan,
    QueryOperation,
    QueryStep,
    ResolvedQuery,
    RetrievalRecord,
    RetrievalSource,
    StepExecutionResult,
    StepExecutionStatus,
)
from app.execution.transforms import InternalTransformExecutor
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.planning.coordinator import QueryPlanner
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    V2SnapshotSelection,
)


def _ontology() -> RDFOntologyService:
    fields = CanonicalV2FieldRegistry().canonical_fields
    return RDFOntologyService(
        OntologyLoader(
            Path("ontology"), known_canonical_fields=fields, version="team-v1"
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


async def _plan(question: str):
    parsed = await RuleBasedQueryAnalyzer().analyze(question)
    grounded = await _ontology().ground(ResolvedQuery(parsed_query=parsed))
    return parsed, grounded, await _planner().create_plan(grounded)


def test_source_metric_contracts_are_source_scoped() -> None:
    assert PREF01_AUM.dataset == "PREF01N001"
    assert PREF01_AUM.currency == "KRW"
    assert PREF02_AUM.dataset == "PREF02N001"
    assert PREF02_AUM.currency == "USD"
    assert not PREF01_AUM.cross_dataset_comparability
    assert not PREF02_AUM.cross_dataset_comparability
    assert not PREF01_EXPENSE.sort_capability
    assert not PREF02_EXPENSE.sort_capability
    assert "scale" in PREF01_EXPENSE.disabled_reason
    assert PRBD_CREDIT_RATING.filter_capability
    assert PRBD_CREDIT_RATING.scale == "CREDIT_RATING_V1"
    assert PRBD_PURCHASABLE_BOND.entity_grain == "Bond"
    assert PRBD_PURCHASABLE_BOND.filter_capability
    assert PREF01_ONE_YEAR_RETURN.sort_capability
    assert PREF01_ONE_YEAR_RETURN.exact_period == "1Y"
    assert not PREF02_ONE_YEAR_RETURN.sort_capability
    assert PRFD_SHARE_CLASS_ONE_YEAR_RETURN.entity_grain == "FundShareClass"
    assert EVALUATION_DATA_CUTOFF == date(2026, 8, 24)
    assert ISHARES_SCOPED_ONE_YEAR_RETURN.sort_capability
    assert ISHARES_SCOPED_ONE_YEAR_RETURN.value_basis == (
        "issuer-published NAV total return"
    )
    assert all(not item.runtime_enabled for item in CROSS_PRODUCT_RETURN_CONTRACTS)
    assert {item.comparability for item in CROSS_PRODUCT_RETURN_CONTRACTS} == {
        "PARTIAL", "NO",
    }


def test_credit_rating_is_an_explicit_non_lexical_order() -> None:
    order = MetricCapabilityRegistry.credit_rating_order
    assert order["AAA"] > order["AA+"] > order["AA0"] > order["AA-"]
    assert order["AA-"] > order["A+"]
    assert "AAAA" not in order


def test_official_bond_semantics_are_structured() -> None:
    _, _, plan = asyncio.run(
        _plan("현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    inputs = plan.steps[0].inputs
    assert inputs["product_types"] == ["FinancialProduct.Bond"]
    fields = {item["canonical_field"]: item for item in inputs["filters"]}
    assert fields["product.currency"]["canonical_value"] == "KRW"
    assert fields["product.credit_rating"]["raw"]["operator"] == "gte"
    assert fields["product.credit_rating"]["canonical_value"] == "AA-"
    assert fields["product.current_sale_available"]["canonical_value"] is True
    assert inputs["ordered_comparisons"] == [
        {
            "semantic_field": "product.credit_rating",
            "operator": "gte",
            "value": "AA-",
        }
    ]
    assert {item["metric"] for item in inputs["comparison_contracts"]} == {
        "CREDIT_RATING_ORDER",
        "ORGANIZER_PURCHASABLE_BOND",
    }


def test_aum_top_n_uses_pref02_contract() -> None:
    _, _, plan = asyncio.run(
        _plan("미국 증시에 상장된 주식형 ETF 중 순자산이 큰 상품 3개")
    )
    inputs = plan.steps[0].inputs
    assert inputs["sort_operations"] == [
        {"semantic_metric_key": "product.aum", "direction": "desc"}
    ]
    assert inputs["top_n"] == {"value": 3}
    assert inputs["comparison_contracts"][0]["dataset"] == "PREF02N001"
    assert inputs["limit"] == 3


def test_one_year_return_top10_is_exact_period_and_source_scoped() -> None:
    parsed, _, plan = asyncio.run(
        _plan("국내 ETF 중 연 수익률 기준 TOP10 알려줘")
    )
    assert parsed.product_universe.operands == ["DomesticETF"]
    inputs = plan.steps[0].inputs
    assert inputs["sort_operations"] == [
        {"semantic_metric_key": "product.one_year_return", "direction": "desc"}
    ]
    assert inputs["top_n"] == {"value": 10}
    assert inputs["comparison_contracts"][0]["metric"] == "ONE_YEAR_RETURN"
    assert inputs["comparison_contracts"][0]["dataset"] == "PREF01N001"


@pytest.mark.parametrize(
    ("question", "operands"),
    [
        ("ETF와 공모펀드 알려줘", ["ETF", "PublicFund"]),
        (
            "국내/해외 ETF와 공모펀드 알려줘",
            ["DomesticETF", "ForeignETF", "PublicFund"],
        ),
    ],
)
def test_validated_product_universe_union_is_explicit(
    question: str, operands: list[str]
) -> None:
    parsed, _, plan = asyncio.run(_plan(question))
    assert parsed.product_universe.operands == operands
    assert plan.steps[0].inputs["product_universe"] == {
        "operation": "UNION",
        "operands": operands,
    }


def test_public_fund_return_is_not_silently_promoted_or_dropped() -> None:
    question = "국내/해외 ETF와 공모펀드 중 1년 수익률이 높은 순으로 10개 알려줘"
    with pytest.raises(UnsupportedQuerySemanticsError) as caught:
        asyncio.run(_plan(question))
    assert (
        "unsupported_comparison:public_fund_one_year_return_is_share_class_grain_only"
        in caught.value.reasons
    )


@pytest.mark.parametrize("security", ["삼성전자", "SK하이닉스"])
def test_holdings_boundary_is_structured_without_production_fact_fabrication(
    security: str,
) -> None:
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze(
            f"{security}를 보유한 국내/해외 ETF와 공모펀드를 "
            "연 수익률 기준 TOP10 알려줘"
        )
    )
    assert parsed.product_universe.operands == [
        "DomesticETF", "ForeignETF", "PublicFund"
    ]
    assert len(parsed.relations) == 1
    relation = parsed.relations[0]
    assert relation.raw_text == "보유한"
    assert relation.target_type == "Organization"
    assert relation.target_value == security
    assert parsed.sort[0].field == "연 수익률"
    assert parsed.result_limit.value == 10

    # C1 prepares the relation boundary but does not invent ontology mappings
    # or production holdings facts.
    with pytest.raises(UnsupportedQuerySemanticsError):
        asyncio.run(_plan(parsed.original_question))


@pytest.mark.parametrize(
    ("word", "direction"),
    [("오름차순", "asc"), ("내림차순", "desc"), ("ASC", "asc"), ("DESC", "desc")],
)
def test_explicit_sort_direction_is_preserved(word: str, direction: str) -> None:
    _, _, plan = asyncio.run(
        _plan(f"미국 증시에 상장된 ETF 중 순자산 기준 {word} 상품 3개")
    )
    assert plan.steps[0].inputs["sort_operations"] == [
        {"semantic_metric_key": "product.aum", "direction": direction}
    ]


@pytest.mark.parametrize(
    "question",
    [
        "순자산이 큰 ETF 3개",
        "미국 증시에 상장된 ETF 중 총보수가 낮은 순으로 3개",
        "상위 3개 ETF",
        "미국 증시에 상장된 ETF 중 순자산이 큰 상품 1001개",
        "현재 판매 가능한 원화채권 중 AAAA 이상 종목 알려줘",
    ],
)
def test_unsupported_comparisons_fail_before_retrieval(question: str) -> None:
    with pytest.raises(UnsupportedQuerySemanticsError):
        asyncio.run(_plan(question))


@pytest.mark.parametrize(
    ("question", "reason"),
    [
        (
            "순자산이 큰 ETF 3개",
            "unsupported_comparison:aum_scope_spans_or_cannot_exclude_incompatible_sources",
        ),
        (
            "미국 증시에 상장된 ETF 중 총보수가 낮은 순으로 3개",
            "unsupported_comparison:expense_ratio_scale_unverified",
        ),
        (
            "현재 판매 가능한 원화채권 중 AAAA 이상 종목 알려줘",
            "unsupported_comparison:invalid_credit_rating",
        ),
    ],
)
def test_unsupported_comparison_exposes_safe_reason(
    question: str, reason: str
) -> None:
    with pytest.raises(UnsupportedQuerySemanticsError) as caught:
        asyncio.run(_plan(question))
    assert reason in caught.value.reasons


def test_compiler_uses_allow_listed_sqlalchemy_ranking_and_stable_tie_break() -> None:
    _, _, plan = asyncio.run(
        _plan("미국 증시에 상장된 주식형 ETF 중 순자산이 큰 상품 3개")
    )
    snapshot = V2SnapshotSelection(
        snapshot_date=date(2026, 8, 24),
        generation="260824",
        ontology_version="merged-optical-1.3",
        snapshot_ids=("pref01", "pref02", "prbd", "prfd"),
        dataset_ids=("PREF01N001", "PREF02N001", "PRBD01N001", "PRFD01N001"),
    )
    compiled = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100
    ).compile(plan.steps[0], snapshot)
    sql = str(
        compiled.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "metric_observations" in sql
    assert "PREF02N001" in sql
    assert "ORDER BY" in sql and "DESC" in sql and "COLLATE \"C\"" in sql
    assert "LIMIT 3" in sql
    assert "미국 증시에" not in sql
    assert compiled.ranking_applied


def test_bond_compiler_ignores_quantity_and_excludes_lifecycle_end_facts() -> None:
    _, _, plan = asyncio.run(
        _plan("현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    snapshot = V2SnapshotSelection(
        snapshot_date=date(2026, 8, 24),
        generation="260824",
        ontology_version="merged-optical-1.3",
        snapshot_ids=("prbd",),
        dataset_ids=("PRBD01N001",),
    )
    compiled = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100
    ).compile(plan.steps[0], snapshot)
    sql = str(
        compiled.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "BOND_DELISTING_DATE" in sql
    assert "BOND_LISTING_END_DATE" in sql
    assert "buyable_quantity" not in sql.casefold()
    assert "CURRENT_SALE_AVAILABILITY" not in sql
    assert "CREDIT_RATING_ORDER" in sql
    assert compiled.result_grain.value == "financial_product"


def test_allowlisted_cross_product_shape_compiles_one_global_ranking() -> None:
    step = QueryStep(
        step_id="cross-product",
        source=RetrievalSource.RDB,
        operation=QueryOperation.SEARCH_PRODUCTS,
        inputs={
            "product_types": [],
            "product_universe": {
                "operation": "UNION",
                "operands": ["DomesticETF", "ForeignETF"],
            },
            "filters": [],
            "relations": [],
            "requested_fields": [],
            "sort": [
                {
                    "raw": {"field": "1년 수익률", "direction": "desc"},
                    "canonical_field": "product.one_year_return",
                }
            ],
            "comparison_contracts": [
                {
                    **PREF01_ONE_YEAR_RETURN.as_plan_input(),
                    "dataset": "PREF01N001",
                    "datasets": ["PREF01N001", "PREF02N001"],
                    "cross_dataset_comparability": True,
                }
            ],
            "limit": 10,
            "top_n": {"value": 10},
        },
    )
    snapshot = V2SnapshotSelection(
        snapshot_date=date(2026, 8, 24), generation="260824",
        ontology_version="merged-optical-1.3",
        snapshot_ids=("pref01", "pref02"),
        dataset_ids=("PREF01N001", "PREF02N001"),
    )
    compiled = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100
    ).compile(step, snapshot)
    sql = str(compiled.statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    assert sql.count("ORDER BY") >= 2  # metric scalar observation + global rank
    assert sql.count("LIMIT 10") == 1
    assert "PREF01N001" in sql and "PREF02N001" in sql


def test_ranked_intersection_preserves_all_metric_evidence_per_top_n_entity() -> None:
    rdb_step = QueryStep(
        step_id="rdb",
        source=RetrievalSource.RDB,
        operation=QueryOperation.SEARCH_PRODUCTS,
    )
    graph_step = QueryStep(
        step_id="graph",
        source=RetrievalSource.GRAPH,
        operation=QueryOperation.RELATIONSHIP_SEARCH,
    )
    rank_step = QueryStep(
        step_id="rank",
        source=RetrievalSource.INTERNAL,
        operation=QueryOperation.RANK_CANDIDATES,
        inputs={
            "limit": 2,
            "sort": [{"canonical_field": "product.one_year_return"}],
            "comparison_contracts": [{"metric": "ONE_YEAR_RETURN"}],
        },
        depends_on=["rdb", "graph"],
    )
    plan = QueryPlan(planner="supervisor", steps=[rdb_step, graph_step, rank_step])
    now = datetime.now(UTC)

    def result(step: QueryStep, records: list[RetrievalRecord], metadata=None):
        return StepExecutionResult(
            step_id=step.step_id,
            source=step.source,
            status=StepExecutionStatus.SUCCESS,
            records=records,
            retrieval_metadata=metadata or {},
            started_at=now,
            finished_at=now,
            duration_seconds=0,
        )

    rdb_records = [
        RetrievalRecord(
            source="rdb",
            source_id=f"rdb:{entity_id}:{field}",
            entity_id=entity_id,
            payload={"field": field, "value": value},
        )
        for entity_id, name, metric in (
            ("etf:1", "first", "20.0"),
            ("etf:2", "second", "10.0"),
            ("etf:3", "third", "5.0"),
        )
        for field, value in (
            ("product.name", name),
            ("product.one_year_return", metric),
        )
    ]
    graph_records = [
        RetrievalRecord(
            source="graph",
            source_id=f"graph:{entity_id}",
            entity_id=entity_id,
            payload={"field": "relation.holds", "value": "security:samsung"},
        )
        for entity_id in ("etf:1", "etf:2", "etf:3")
    ]
    context = ExecutionContext(
        plan=plan,
        step_results={
            "rdb": result(
                rdb_step,
                rdb_records,
                {
                    "rankable_total": 3,
                    "returned_count": 3,
                    "ranked_candidate_ids": ["etf:1", "etf:2", "etf:3"],
                },
            ),
            "graph": result(graph_step, graph_records),
        },
    )

    records = asyncio.run(InternalTransformExecutor().execute(rank_step, context))

    assert [record.entity_id for record in records] == [
        "etf:1", "etf:1", "etf:2", "etf:2"
    ]
    assert [record.payload["field"] for record in records] == [
        "product.name", "product.one_year_return",
        "product.name", "product.one_year_return",
    ]
    assert all(record.metadata["ranking_applied"] for record in records)
