"""Risk labels remain facts; no supplied source contract authorizes comparison."""

import asyncio
import json

import pytest
from sqlalchemy.dialects import postgresql

from app.data.metric_capabilities import (
    MetricCapabilityRegistry, RISK_GRADE_ORDER, RISK_GRADE_UNVERIFIED_REASON,
)
from app.domain.models import QueryStep
from app.graph.compiler import GraphQueryCompiler
from app.graph.mapping import GraphMappingRegistry
from app.retrieval.exceptions import GraphQueryCompilationError, RDBQueryCompilationError
from app.retrieval.rdb import RDBFieldRegistry, RDBQueryCompiler
from app.planning.exceptions import UnsupportedQuerySemanticsError
from tests.evidence_helpers import make_bundle, make_evidence, validate
from tests.test_m10_5_semantic_safety import _tiger_plan
from tests.test_m10_9_c1_structured_operations import _plan
from tests.test_semantic_composition_rdb import SNAPSHOT, _compiler, _filter, _step


RISK = "product.risk_grade"


def _compile(inputs, backend):
    step = _step(inputs)
    if backend == "v2":
        return _compiler().compile(step, SNAPSHOT)
    return RDBQueryCompiler(
        RDBFieldRegistry(), default_limit=10, snapshot_date="2026-08-24",
    ).compile(step)


@pytest.mark.parametrize("backend", ["v1", "v2"])
def test_single_entity_risk_projection_compiles_without_order_contract(backend):
    _, _, grounded, plan = asyncio.run(
        _tiger_plan("TIGER 미국S&P500 ETF의 위험 정보 알려줘")
    )
    inputs = plan.steps[0].inputs
    compiled = _compile(inputs, backend)
    assert compiled.projected_fields == (RISK,)
    assert not compiled.ranking_applied
    assert not inputs.get("comparison_contracts")
    assert not inputs.get("sort")
    sql = compiled.statement.compile(dialect=postgresql.dialect())
    assert inputs["entity_ids"][0] in sql.params.values() or any(
        inputs["entity_ids"] == value for value in sql.params.values()
    )
    assert grounded.grounded_requested_fields[0].canonical_field == RISK


@pytest.mark.parametrize("grade", range(1, 7))
def test_source_risk_label_is_answerable_without_invented_ordinal_unit(grade):
    _, _, grounded, plan = asyncio.run(
        _tiger_plan("TIGER 미국S&P500 ETF의 위험 정보 알려줘")
    )
    fact = make_evidence(field=RISK, value=f"RiskGrade.{grade}").model_copy(
        update={"entity_id": plan.steps[0].inputs["entity_ids"][0]}
    )
    result = validate(grounded, make_bundle([fact]))
    assert result.answerable, result.findings


@pytest.mark.parametrize("backend", ["v1", "v2"])
@pytest.mark.parametrize("operator", ["eq", "ne", "gt", "gte", "lt", "lte", "in", "between", "contains"])
def test_risk_predicate_is_rejected_before_database_io(backend, operator):
    value = ["RiskGrade.1", "RiskGrade.2"] if operator in {"in", "between"} else "RiskGrade.1"
    with pytest.raises(RDBQueryCompilationError, match=RISK_GRADE_UNVERIFIED_REASON):
        _compile({"filters": [_filter(value, field=RISK, operator=operator)]}, backend)


@pytest.mark.parametrize("backend", ["v1", "v2"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_risk_sort_rejects_forged_previous_contract(backend, direction):
    forged = RISK_GRADE_ORDER.as_plan_input() | {
        "sort_capability": True, "cross_dataset_comparability": True,
        "disabled_reason": None, "comparison_kind": "ordered_vocabulary",
        "ordered_values": [f"RiskGrade.{grade}" for grade in range(6, 0, -1)],
        "unit": "ORDINAL", "scale": "TEAM_ONTOLOGY_RISK_GRADE_V1",
    }
    inputs = json.loads(json.dumps({
        "sort": [{"canonical_field": RISK, "raw": {"field": RISK, "direction": direction}}],
        "comparison_contracts": [forged],
    }))
    with pytest.raises(RDBQueryCompilationError, match=RISK_GRADE_UNVERIFIED_REASON):
        _compile(inputs, backend)


@pytest.mark.parametrize("backend", ["v1", "v2"])
@pytest.mark.parametrize("universe", [
    ["DomesticETF"], ["Bond"], ["PublicFund"],
    ["DomesticETF", "Bond", "PublicFund"], [],
])
def test_same_source_and_cross_source_risk_comparison_are_disabled(backend, universe):
    inputs = {
        "product_universe": {"operation": "UNION", "operands": universe},
        "comparison": {"mode": "fieldwise", "fields": [RISK]},
    }
    assert MetricCapabilityRegistry().comparison_contract(RISK, inputs) == (
        None, RISK_GRADE_UNVERIFIED_REASON,
    )
    with pytest.raises(RDBQueryCompilationError, match=RISK_GRADE_UNVERIFIED_REASON):
        _compile(inputs, backend)


@pytest.mark.parametrize("version", ["legacy", "canonical-v2"])
@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
def test_risk_graph_relation_cannot_bypass_filter_policy(version, direction):
    compiler = GraphQueryCompiler(GraphMappingRegistry(version=version), snapshot="2026-08-24")
    path = {"relations": ["hasRiskGrade"], "directions": [direction],
            "target_values": ["RiskGrade.1"]}
    step = QueryStep(step_id="risk", source="graph", operation="relationship_search",
                     inputs={"paths": [path]})
    with pytest.raises(GraphQueryCompilationError, match=RISK_GRADE_UNVERIFIED_REASON):
        compiler.compile(step, path)


@pytest.mark.parametrize("question", [
    "위험등급 1등급인 ETF를 알려줘",
    "위험이 낮은 채권형 상품을 비교해줘",
])
def test_natural_language_risk_selection_is_not_silently_dropped(question):
    with pytest.raises(UnsupportedQuerySemanticsError):
        asyncio.run(_plan(question))
