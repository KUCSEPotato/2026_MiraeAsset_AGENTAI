from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.data.metric_capabilities import MetricCapabilityRegistry
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
from app.retrieval.rdb_v2 import CanonicalV2FieldRegistry
from app.domain.models import ResolvedQuery, ResolutionStatus


ISHARES_SCOPE = "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS"


async def _plan(question: str):
    analyzer = RuleBasedQueryAnalyzer()
    parsed = await analyzer.analyze(question)
    resolved_entities = [
        item.model_copy(update={
            "canonical_id": f"security:test:{item.raw_text}",
            "resolution_status": ResolutionStatus.RESOLVED,
            "confidence": 1.0,
        })
        for item in parsed.entities
    ]
    ontology = RDFOntologyService(OntologyLoader(
        Path("ontology"),
        known_canonical_fields=CanonicalV2FieldRegistry().canonical_fields,
        version="team-v1",
    ).load())
    grounded = await ontology.ground(ResolvedQuery(
        parsed_query=parsed, resolved_entities=resolved_entities,
    ))
    metadata = RoutingMetadataRegistry()
    planner = QueryPlanner(
        routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata),
    )
    return parsed, grounded, await planner.create_plan(grounded)


def test_scoped_ishares_return_ranking_uses_source_contract() -> None:
    parsed, _, plan = asyncio.run(_plan(
        "검증된 iShares 해외 ETF 범위에서 1년 수익률이 높은 순으로 알려줘"
    ))
    assert parsed.product_universe.operands == [ISHARES_SCOPE]
    inputs = plan.steps[0].inputs
    assert inputs["sort_operations"] == [{
        "semantic_metric_key": "product.one_year_return",
        "direction": "desc",
    }]
    assert inputs["comparison_contracts"][0]["dataset"] == (
        "ISHARES_US_PERFORMANCE"
    )
    assert inputs["comparison_contracts"][0]["value_basis"] == (
        "issuer-published NAV total return"
    )


def test_holdings_and_return_constraints_compose_without_special_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(_plan(
        "검증된 iShares ETF 보유종목 범위에서 XNAS:NVDA를 보유한 ETF 중 "
        "1년 수익률이 높은 순으로 알려줘"
    ))
    assert parsed.semantic_coverage == "complete"
    rdb_step = next(step for step in plan.steps if step.source.value == "rdb")
    inputs = rdb_step.inputs
    assert inputs["product_universe"]["operands"] == [ISHARES_SCOPE]
    graph_step = next(step for step in plan.steps if step.step_id == "graph-relations")
    assert graph_step.inputs["paths"][0]["relations"] == ["holds"]
    assert plan.steps.index(graph_step) < plan.steps.index(rdb_step)
    assert rdb_step.depends_on == [graph_step.step_id]
    assert inputs["candidate_ids_from"] == [graph_step.step_id]
    assert graph_step.inputs["require_complete_candidates"] is True
    assert inputs["sort_operations"][0]["semantic_metric_key"] == (
        "product.one_year_return"
    )


def test_generic_foreign_return_ranking_fails_closed() -> None:
    with pytest.raises(UnsupportedQuerySemanticsError) as raised:
        asyncio.run(_plan("해외 ETF 중 1년 수익률 TOP10"))
    assert any(
        "foreign_etf_return_1Y_unavailable_or_incompatible" in item
        for item in raised.value.reasons
    )


def test_domestic_ishares_return_union_is_not_comparable() -> None:
    contract, reason = MetricCapabilityRegistry().comparison_contract(
        "product.one_year_return",
        {"product_universe": {"operands": [
            "KODEX_LONG_ONLY_COMPATIBLE",
            "TIGER_LONG_ONLY_COMPATIBLE",
            ISHARES_SCOPE,
        ]}},
    )
    assert contract is None
    assert reason == "domestic_vs_ishares_return_basis_not_comparable"
