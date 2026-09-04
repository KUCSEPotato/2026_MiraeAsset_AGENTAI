from __future__ import annotations

import asyncio
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.engine import make_url

from app.data.v2_schema import (
    bonds,
    canonical_entities,
    canonical_facts,
    canonical_scalar_facts,
    fact_evidence_links,
    financial_products,
    fund_share_classes,
    metric_observations,
    sale_lots,
)
from app.domain.models import ExecutionContext, ResolvedQuery
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.planning.coordinator import QueryPlanner
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    CanonicalV2RDBRetriever,
    CanonicalV2SnapshotSelector,
)


pytestmark = pytest.mark.postgresql


def _url() -> str:
    value = os.getenv("M10_9_C1_DATABASE_URL")
    if not value:
        pytest.skip("M10_9_C1_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("C1 integration requires PostgreSQL")
    if "test" not in (parsed.database or "").casefold():
        pytest.fail("C1 integration requires a disposable test database")
    return value


@pytest.fixture(scope="module")
def engine():
    value = create_engine(_url())
    yield value
    value.dispose()


@pytest.fixture(scope="module")
def runtime(engine):
    fields = CanonicalV2FieldRegistry()
    ontology = RDFOntologyService(
        OntologyLoader(
            Path("ontology"),
            known_canonical_fields=fields.canonical_fields,
            version="team-v1",
        ).load()
    )
    metadata = RoutingMetadataRegistry()
    planner = QueryPlanner(
        routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata),
    )
    selector = CanonicalV2SnapshotSelector(snapshot_date="2026-08-24")
    compiler = CanonicalV2QueryCompiler(fields, default_limit=100, max_limit=10_000)
    retriever = CanonicalV2RDBRetriever(engine, compiler, selector)
    return ontology, planner, selector, compiler, retriever


async def _execute(runtime, question: str):
    ontology, planner, _, _, retriever = runtime
    parsed = await RuleBasedQueryAnalyzer().analyze(question)
    grounded = await ontology.ground(ResolvedQuery(parsed_query=parsed))
    plan = await planner.create_plan(grounded)
    result = await retriever.retrieve_with_result(
        plan.steps[0], ExecutionContext(plan=plan)
    )
    return plan, result


def test_full_data_aum_top3_cardinality_order_and_evidence(runtime) -> None:
    _, result = asyncio.run(
        _execute(runtime, "미국 증시에 상장된 주식형 ETF 중 순자산이 큰 상품 3개")
    )
    _, repeated = asyncio.run(
        _execute(runtime, "미국 증시에 상장된 주식형 ETF 중 순자산이 큰 상품 3개")
    )
    assert result.total_matches == result.filtered_total == 2_587
    assert result.rankable_total == 2_496
    assert result.missing_metric_total == 91
    assert result.returned_count == 3
    assert result.requested_top_n == 3
    assert result.ranked_candidate_ids == ["etf_gl:VOO", "etf_gl:IVV", "etf_gl:SPY"]
    assert repeated.ranked_candidate_ids == result.ranked_candidate_ids
    metrics = [record for record in result.records if record.payload["field"] == "product.aum"]
    assert [record.payload["value"] for record in metrics] == sorted(
        [record.payload["value"] for record in metrics], reverse=True
    )
    assert all(record.metadata["metric_currency"] == "USD" for record in metrics)
    assert all(record.metadata["metric_dataset"] == "PREF02N001" for record in metrics)
    assert all(record.metadata["metric_scale_basis"] == "CURRENCY_UNIT" for record in metrics)
    assert all(record.metadata["field_fact_id"] for record in metrics)
    assert all(record.metadata["field_evidence_assertion_ids"] for record in metrics)


def test_full_data_bond_rating_and_current_availability(runtime) -> None:
    _, result = asyncio.run(
        _execute(runtime, "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    assert result.total_matches == result.filtered_total == 15_848
    assert result.rankable_total == 15_848
    assert result.missing_metric_total == 0
    assert result.returned_count == 100
    assert len({record.entity_id for record in result.records}) == 100
    ratings = [
        record for record in result.records
        if record.payload["field"] == "product.credit_rating"
    ]
    assert {record.payload["value"] for record in ratings} <= {
        "AA-", "AA0", "AA+", "AAA"
    }
    assert all(record.metadata["field_fact_id"] for record in ratings)
    assert all(record.metadata["field_evidence_assertion_ids"] for record in ratings)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("현재 구매 가능한 채권", 20_497),
        ("현재 구매 가능한 장내채권", 17_746),
        ("현재 구매 가능한 장외채권", 3_828),
        ("미래에셋 판매조건이 있는 채권", 326),
        ("매매단가와 수익률이 제공된 장외채권", 326),
        ("판매 LOT은 없지만 구매 가능한 채권", 20_171),
        ("하나의 종목에 여러 판매조건이 있는 채권", 307),
        ("상장폐지 또는 리스팅 종료 채권 제외", 20_497),
    ],
)
def test_full_data_bond_query_contract_cardinality(
    runtime, question: str, expected: int
) -> None:
    _, result = asyncio.run(_execute(runtime, question))
    assert result.total_matches == result.filtered_total == expected
    assert all(
        record.metadata["entity_kind"] == "FINANCIAL_PRODUCT"
        for record in result.records
    )


def test_public_fund_subscription_and_freshness_cardinality(runtime) -> None:
    _, eligible = asyncio.run(
        _execute(runtime, "현재 미래에셋에서 가입할 수 있는 공모펀드")
    )
    _, fresh = asyncio.run(
        _execute(runtime, "추가매수 가능한 펀드 중 최신 기준가가 있는 상품")
    )
    # Snapshot-specific regression values for PRFD01N001_20260824 only.
    assert eligible.total_matches == eligible.filtered_total == 8_550
    assert fresh.total_matches == fresh.filtered_total == 7_315
    assert all(record.metadata["entity_kind"] == "FUND_SHARE_CLASS" for record in eligible.records)
    assert all(record.metadata["entity_kind"] == "FUND_SHARE_CLASS" for record in fresh.records)
    assert fresh.total_matches < eligible.total_matches


def test_buyable_quantity_is_ignored(engine, runtime) -> None:
    _, _, selector, compiler, _ = runtime
    plan, _ = asyncio.run(
        _execute(runtime, "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    with engine.connect() as connection:
        transaction = connection.begin()
        snapshot = selector.select(connection)
        compiled = compiler.compile(plan.steps[0], snapshot)
        before = int(connection.scalar(compiled.count_statement) or 0)
        bond_id = connection.execute(compiled.statement).mappings().first()["entity_id"]
        snapshot_id = next(
            value for value in snapshot.snapshot_ids if "PRBD01N001" in value
        )
        fact_id = "fact:c1-ignored-buyable-quantity"
        connection.execute(insert(canonical_facts).values(
            fact_id=fact_id, subject_entity_id=bond_id, snapshot_id=snapshot_id,
            fact_kind="SCALAR", semantic_key="BUYABLE_QUANTITY",
            resolution_status="RESOLVED",
        ))
        connection.execute(insert(canonical_scalar_facts).values(
            fact_id=fact_id, value_type="NUMERIC", numeric_value=Decimal(0),
        ))
        assert int(connection.scalar(compiled.count_statement) or 0) == before
        transaction.rollback()


@pytest.mark.parametrize("semantic_key", ["BOND_DELISTING_DATE", "BOND_LISTING_END_DATE"])
def test_lifecycle_end_excludes_bond(engine, runtime, semantic_key: str) -> None:
    _, _, selector, compiler, _ = runtime
    plan, _ = asyncio.run(
        _execute(runtime, "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    with engine.connect() as connection:
        transaction = connection.begin()
        snapshot = selector.select(connection)
        compiled = compiler.compile(plan.steps[0], snapshot)
        before = int(connection.scalar(compiled.filtered_count_statement) or 0)
        bond_id = connection.execute(compiled.statement).mappings().first()["entity_id"]
        snapshot_id = next(
            value for value in snapshot.snapshot_ids if "PRBD01N001" in value
        )
        fact_id = f"fact:c1-{semantic_key.casefold()}"
        connection.execute(insert(canonical_facts).values(
            fact_id=fact_id, subject_entity_id=bond_id, snapshot_id=snapshot_id,
            fact_kind="SCALAR", semantic_key=semantic_key,
            resolution_status="RESOLVED",
        ))
        connection.execute(insert(canonical_scalar_facts).values(
            fact_id=fact_id, value_type="DATE", date_value=date(2026, 8, 24),
        ))
        assert int(connection.scalar(compiled.filtered_count_statement) or 0) == before - 1
        transaction.rollback()


def test_multiple_sale_lots_do_not_duplicate_or_define_purchasability(
    engine, runtime
) -> None:
    _, _, selector, compiler, _ = runtime
    plan, _ = asyncio.run(
        _execute(runtime, "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘")
    )
    with engine.connect() as connection:
        transaction = connection.begin()
        snapshot = selector.select(connection)
        compiled = compiler.compile(plan.steps[0], snapshot)
        before = int(connection.scalar(compiled.filtered_count_statement) or 0)
        bond_id = connection.execute(compiled.statement).mappings().first()["entity_id"]
        lot_id = "sale_lot:c1-organizer-dedup-test"
        connection.execute(insert(canonical_entities).values(
            entity_id=lot_id, entity_kind="SALE_LOT", preferred_name="C1 test lot",
            name_status="SOURCE_ONLY", identity_status="VALIDATED", query_eligible=True,
        ))
        connection.execute(insert(sale_lots).values(
            sale_lot_id=lot_id, bond_id=bond_id, trading_market_raw="TEST",
            information_date=date(2026, 8, 24), lot_sequence=999,
        ))
        assert int(connection.scalar(compiled.filtered_count_statement) or 0) == before
        transaction.rollback()


def test_past_maturity_does_not_define_bond_purchasability(engine, runtime) -> None:
    _, _, selector, compiler, _ = runtime
    plan, _ = asyncio.run(_execute(runtime, "현재 구매 가능한 채권"))
    with engine.connect() as connection:
        transaction = connection.begin()
        snapshot = selector.select(connection)
        compiled = compiler.compile(plan.steps[0], snapshot)
        before = int(connection.scalar(compiled.filtered_count_statement) or 0)
        bond_id = connection.execute(compiled.statement).mappings().first()["entity_id"]
        connection.execute(
            bonds.update().where(bonds.c.bond_id == bond_id).values(
                maturity_date=date(2000, 1, 1)
            )
        )
        assert int(connection.scalar(compiled.filtered_count_statement) or 0) == before
        transaction.rollback()


def test_zero_remaining_days_does_not_define_bond_purchasability(
    engine, runtime
) -> None:
    _, _, selector, compiler, _ = runtime
    plan, _ = asyncio.run(_execute(runtime, "현재 구매 가능한 채권"))
    with engine.connect() as connection:
        transaction = connection.begin()
        snapshot = selector.select(connection)
        compiled = compiler.compile(plan.steps[0], snapshot)
        before = int(connection.scalar(compiled.filtered_count_statement) or 0)
        bond_id = connection.execute(compiled.statement).mappings().first()["entity_id"]
        snapshot_id = next(
            value for value in snapshot.snapshot_ids if "PRBD01N001" in value
        )
        fact_id = "fact:c1-ignored-remaining-days"
        connection.execute(insert(canonical_facts).values(
            fact_id=fact_id, subject_entity_id=bond_id, snapshot_id=snapshot_id,
            fact_kind="SCALAR", semantic_key="remaining_days",
            resolution_status="RESOLVED",
        ))
        connection.execute(insert(canonical_scalar_facts).values(
            fact_id=fact_id, value_type="NUMERIC", numeric_value=Decimal(0),
        ))
        assert int(connection.scalar(compiled.filtered_count_statement) or 0) == before
        transaction.rollback()


def test_domestic_etf_one_year_return_top10_cardinality_and_evidence(runtime) -> None:
    _, result = asyncio.run(
        _execute(runtime, "국내 ETF 중 연 수익률 기준 TOP10 알려줘")
    )
    _, repeated = asyncio.run(
        _execute(runtime, "국내 ETF 중 연 수익률 기준 TOP10 알려줘")
    )
    assert result.total_matches == result.filtered_total == 1_234
    assert result.rankable_total == 981
    assert result.missing_metric_total == 253
    assert result.returned_count == result.requested_top_n == 10
    assert result.ranked_candidate_ids == repeated.ranked_candidate_ids
    metrics = [
        record for record in result.records
        if record.payload["field"] == "product.one_year_return"
    ]
    values = [record.payload["value"] for record in metrics]
    assert values == sorted(values, reverse=True)
    assert all(record.metadata["metric_dataset"] == "PREF01N001" for record in metrics)
    assert all(record.metadata["metric_unit"] == "PERCENT" for record in metrics)
    assert all(record.metadata["metric_scale_basis"] == "SOURCE_PERCENT" for record in metrics)
    assert all(record.metadata["field_fact_id"] for record in metrics)
    assert all(record.metadata["field_evidence_assertion_ids"] for record in metrics)


def test_one_year_return_ties_use_canonical_id_order(runtime) -> None:
    ontology, planner, _, _, retriever = runtime
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze(
            "국내 ETF 중 연 수익률 기준 TOP10 알려줘"
        )
    )
    grounded = asyncio.run(ontology.ground(ResolvedQuery(parsed_query=parsed)))
    plan = asyncio.run(planner.create_plan(grounded))
    expected = ["etf_kr:KR7294400007", "etf_kr:KR7295040000"]
    step = plan.steps[0].model_copy(
        update={
            "inputs": {
                **plan.steps[0].inputs,
                "entity_ids": list(reversed(expected)),
                "limit": 2,
                "top_n": {"value": 2},
            }
        }
    )
    result = asyncio.run(
        retriever.retrieve_with_result(step, ExecutionContext(plan=plan))
    )
    assert result.filtered_total == result.rankable_total == 2
    assert result.ranked_candidate_ids == expected


def test_one_year_return_remains_at_authoritative_entity_grain(engine) -> None:
    with engine.connect() as connection:
        fund_level = int(connection.scalar(
            select(func.count()).select_from(metric_observations)
            .join(canonical_entities)
            .join(financial_products, financial_products.c.product_id == canonical_entities.c.entity_id)
            .where(
                metric_observations.c.metric_code == "ONE_YEAR_RETURN",
                financial_products.c.product_type_code == "FUND",
            )
        ) or 0)
        class_level = int(connection.scalar(
            select(func.count()).select_from(metric_observations)
            .join(fund_share_classes, fund_share_classes.c.fund_share_class_id == metric_observations.c.subject_entity_id)
            .where(metric_observations.c.metric_code == "ONE_YEAR_RETURN")
        ) or 0)
        conflicting_families = int(connection.scalar(
            select(func.count()).select_from(
                select(fund_share_classes.c.parent_fund_id)
                .join(metric_observations, metric_observations.c.subject_entity_id == fund_share_classes.c.fund_share_class_id)
                .where(metric_observations.c.metric_code == "ONE_YEAR_RETURN")
                .group_by(fund_share_classes.c.parent_fund_id)
                .having(func.count(func.distinct(metric_observations.c.numeric_value)) > 1)
                .subquery()
            )
        ) or 0)
    assert fund_level == 0
    assert class_level == 7_001
    assert conflicting_families > 0


def test_metric_observations_respect_evaluation_cutoff(engine) -> None:
    with engine.connect() as connection:
        post_cutoff = int(connection.scalar(
            select(func.count()).select_from(metric_observations).where(
                metric_observations.c.observed_on > date(2026, 8, 24)
            )
        ) or 0)
    assert post_cutoff == 0


def test_new_metric_facts_have_field_evidence(engine) -> None:
    with engine.connect() as connection:
        for metric_code in ("CREDIT_RATING_ORDER", "ONE_YEAR_RETURN"):
            facts = int(connection.scalar(
                select(func.count()).select_from(metric_observations).where(
                    metric_observations.c.metric_code == metric_code
                )
            ) or 0)
            links = int(connection.scalar(
                select(func.count()).select_from(
                    fact_evidence_links.join(
                        metric_observations,
                        metric_observations.c.fact_id
                        == fact_evidence_links.c.fact_id,
                    )
                )
                .where(metric_observations.c.metric_code == metric_code)
            ) or 0)
            assert facts > 0
            assert links >= facts
