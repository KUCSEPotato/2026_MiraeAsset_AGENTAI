from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import and_, create_engine, select
from sqlalchemy.engine import make_url

from app.data.metric_capabilities import ISHARES_SCOPED_ONE_YEAR_RETURN
from app.agent.service import _assert_v2_metric_ready
from app.data.v2_schema import (
    canonical_entities,
    canonical_facts,
    entity_identifiers,
    external_metric_records,
    external_raw_artifacts,
    external_source_records,
    fact_evidence_links,
    metric_observations,
    source_field_assertions,
)
from app.domain.models import (
    ExecutionContext,
    PlannerType,
    QueryOperation,
    QueryPlan,
    QueryStep,
    RetrievalSource,
)
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    CanonicalV2RDBRetriever,
    CanonicalV2SnapshotSelector,
)


pytestmark = pytest.mark.postgresql
ISHARES_SCOPE = "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS"


def _url() -> str:
    value = os.getenv("M10_9_C3_DATABASE_URL")
    if not value:
        pytest.skip("M10_9_C3_DATABASE_URL is not configured")
    parsed = make_url(value)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("C3 integration requires PostgreSQL")
    if not {"test", "audit", "c3"} & set((parsed.database or "").casefold().split("_")):
        pytest.fail("C3 integration requires a disposable database")
    return value


@pytest.fixture(scope="module")
def runtime():
    engine = create_engine(_url(), future=True)
    selector = CanonicalV2SnapshotSelector(
        snapshot_date="2026-08-24",
        include_trusted_holdings=True,
        trusted_holdings_scopes=(ISHARES_SCOPE,),
        include_trusted_metrics=True,
        trusted_metric_scopes=("ISHARES_FOREIGN_ETF_ONE_YEAR_RETURN",),
    )
    retriever = CanonicalV2RDBRetriever(
        engine,
        CanonicalV2QueryCompiler(
            CanonicalV2FieldRegistry(), default_limit=10, max_limit=10_000,
        ),
        selector,
    )
    yield engine, retriever
    engine.dispose()


def _inputs() -> dict:
    return {
        "product_types": ["FinancialProduct.ETF"],
        "product_universe": {
            "operation": "UNION", "operands": [ISHARES_SCOPE],
        },
        "filters": [],
        "sort": [{
            "raw": {"field": "1년 수익률", "direction": "desc"},
            "canonical_field": "product.one_year_return",
        }],
        "requested_fields": [
            "product.name", "product.ticker", "product.one_year_return",
        ],
        "comparison_contracts": [
            ISHARES_SCOPED_ONE_YEAR_RETURN.as_plan_input()
        ],
        "limit": 10,
    }


async def _retrieve(retriever, inputs):
    step = QueryStep(
        step_id="rdb-candidates", source=RetrievalSource.RDB,
        operation=QueryOperation.SEARCH_PRODUCTS, inputs=inputs,
    )
    plan = QueryPlan(planner=PlannerType.RULE, steps=[step])
    return await retriever.retrieve_with_result(step, ExecutionContext(plan=plan))


def test_scoped_foreign_return_ranking_and_cardinality(runtime) -> None:
    _, retriever = runtime
    result = asyncio.run(_retrieve(retriever, _inputs()))
    assert result.total_matches == result.rankable_total == 3
    assert result.missing_metric_total == 0
    assert result.returned_count == 3
    assert result.ranked_candidate_ids == [
        "etf_gl:EWY", "etf_gl:SOXX.O", "etf_gl:IYW",
    ]
    records = [
        item for item in result.records
        if item.payload["field"] == "product.one_year_return"
    ]
    assert [item.payload["value"] for item in records] == sorted(
        [item.payload["value"] for item in records], reverse=True,
    )
    assert all(item.metadata["field_fact_id"] for item in records)
    assert all(item.metadata["field_evidence_assertion_ids"] for item in records)


def test_holdings_and_metric_compose_in_allowlisted_compiler(runtime) -> None:
    engine, retriever = runtime
    with engine.connect() as connection:
        target = connection.scalar(
            select(entity_identifiers.c.entity_id)
            .join(
                canonical_entities,
                canonical_entities.c.entity_id == entity_identifiers.c.entity_id,
            )
            .where(and_(
                entity_identifiers.c.scheme_code == "TICKER",
                entity_identifiers.c.namespace == "XNAS",
                entity_identifiers.c.normalized_value == "NVDA",
                canonical_entities.c.entity_kind == "SECURITY",
            ))
        )
    inputs = _inputs()
    inputs["relations"] = [{
        "canonical_relation": "holds",
        "target_entity_id": target,
        "negated": False,
    }]
    result = asyncio.run(_retrieve(retriever, inputs))
    assert result.total_matches == result.rankable_total == 2
    assert result.ranked_candidate_ids == ["etf_gl:SOXX.O", "etf_gl:IYW"]


def test_metric_fact_has_complete_external_evidence_chain(runtime) -> None:
    engine, _ = runtime
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                metric_observations.c.subject_entity_id,
                canonical_facts.c.fact_id,
                fact_evidence_links.c.assertion_id,
                external_metric_records.c.external_source_record_id,
                external_raw_artifacts.c.sha256,
            )
            .join(
                canonical_facts,
                canonical_facts.c.fact_id == metric_observations.c.fact_id,
            )
            .join(
                fact_evidence_links,
                fact_evidence_links.c.fact_id == canonical_facts.c.fact_id,
            )
            .join(
                source_field_assertions,
                source_field_assertions.c.assertion_id
                == fact_evidence_links.c.assertion_id,
            )
            .join(
                external_metric_records,
                external_metric_records.c.canonical_source_record_id
                == source_field_assertions.c.source_record_id,
            )
            .join(
                external_source_records,
                external_source_records.c.external_source_record_id
                == external_metric_records.c.external_source_record_id,
            )
            .join(
                external_raw_artifacts,
                external_raw_artifacts.c.artifact_id
                == external_source_records.c.artifact_id,
            )
            .where(
                canonical_facts.c.snapshot_id
                == "snapshot:ishares-us-one-year-return:20260824:v1"
            )
        ).all()
    assert len(rows) == 3
    assert len({row[0] for row in rows}) == 3
    assert all(len(row[4]) == 64 for row in rows)
    _assert_v2_metric_ready(engine)
