"""Production one-off failures replayed locally without changing capabilities."""

import asyncio
import json
import logging

import httpx
import pytest

from app.agent.service import create_pipeline_answer_service
from app.domain.models import CanonicalEntity, ValidationResult
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.evidence.answer import DeterministicEvidenceAnswerGenerator, satisfies_answer_contract
from app.evidence.llm_answer import HyperCLOVAEvidenceAnswerGenerator, _evidence_payload
from app.operations import JsonLogFormatter
from app.planning.exceptions import UnsupportedQuerySemanticsError
from tests.evidence_helpers import make_bundle, make_evidence
from tests.test_m10_9_c1_structured_operations import _ontology, _planner
from tests.test_m10_9_hyperclova_answer import (
    _FailingSemanticParserLLM, _semantic_coordinator, _settings,
)


@pytest.mark.parametrize("interpretation", [
    "중간 위험", "낮은 위험", "높은 위험", "1~5등급", "1~6등급",
    "일반적으로 1~5등급 체계에서 중간 수준의 위험을 의미할 수 있습니다.",
    "1등급보다 안전합니다.", "6등급보다는 위험합니다.", "moderate risk",
    "위험이 적은 편입니다.", "안정적인 성향의 투자자에게 적합합니다.",
])
def test_risk_grade_generated_interpretations_never_escape_value_only_contract(interpretation):
    bundle = make_bundle([
        make_evidence(field="product.risk_grade", value="RiskGrade.2"),
        make_evidence(field="product.aum", value="1000"),
    ])
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"message": {
            "content": f"RiskGrade.2: {interpretation}",
        }}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            generator = HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client)
            return await generator.generate("어떤 상품의 위험 정보", bundle, ValidationResult(answerable=True))

    result = asyncio.run(run())
    assert "RiskGrade.2" in result and "1000" in result
    assert interpretation not in result
    contract = json.loads(_evidence_payload(bundle))["answer_contract"]
    assert contract["value_only_fields"] == ["product.risk_grade"]
    assert contract["risk_grade_ordering_allowed"] is False
    assert "1~5" in captured[0]["messages"][0]["content"]
    assert not satisfies_answer_contract(interpretation, result, bundle)
    assert satisfies_answer_contract(result, result, bundle)


@pytest.mark.parametrize("value", ["RiskGrade.2", "2등급", "제공기관 분류 B"])
def test_deterministic_risk_answer_preserves_raw_display_value_and_ignores_stale_order_metadata(value):
    fact = make_evidence(field="product.risk_grade", value=value, metadata={
        "comparison_contracts": [{"answer_disclosure": "중간 위험, 1~5등급"}],
        "metric_unit": "PERCENT",
    })
    result = asyncio.run(DeterministicEvidenceAnswerGenerator().generate(
        "위험 정보", make_bundle([fact]), ValidationResult(answerable=True),
    ))
    assert value in result
    assert "중간 위험" not in result and "1~5등급" not in result and "%" not in result


@pytest.mark.parametrize("manager", ["미래에셋", "삼성", "가상운용사"])
@pytest.mark.parametrize("period,expected", [("1년", "1Y"), ("6개월", "6M")])
def test_manager_universe_return_ranking_composition_stays_on_rule_parser(manager, period, expected):
    llm = _FailingSemanticParserLLM()
    parsed = asyncio.run(_semantic_coordinator(llm).analyze(
        f"{manager} ETF 중 최근 {period} 수익률이 높은 상위 5개 알려줘",
    ))
    assert llm.calls == 0 and not parsed.unparsed_material_spans
    assert [(entity.raw_text, entity.entity_type) for entity in parsed.entities] == [(manager, "management_company")]
    assert parsed.product_types == ["ETF"]
    assert parsed.relations[0].semantic_key == "운용사"
    assert parsed.relations[0].target_value == manager
    assert parsed.sort[0].direction == "desc" and parsed.result_limit.value == 5
    assert parsed.metrics[0].temporal.period == expected
    assert parsed.metrics[0].temporal.period_source == "EXPLICIT_QUERY"
    for item in parsed.semantic_constraints:
        if item.semantic_type.value != "intent":
            assert parsed.original_question[item.source_span.start:item.source_span.end] == item.raw_text


@pytest.mark.parametrize("manager", ["미래에셋", "삼성"])
def test_manager_with_explicit_domestic_scope_reaches_existing_rdb_ranking(manager):
    async def run():
        llm = _FailingSemanticParserLLM()
        parsed = await _semantic_coordinator(llm).analyze(
            f"{manager} 국내 ETF 중 최근 1년 수익률 상위 5개",
        )
        resolved = await RegistryEntityResolver(StaticEntityLookup([
            CanonicalEntity(canonical_id="manager:fixture", entity_type="management_company", official_name=manager),
        ])).resolve(parsed)
        grounded = await _ontology().ground(resolved)
        return await _planner().create_plan(grounded)
    plan = asyncio.run(run())
    assert any(step.inputs.get("sort_operations") for step in plan.steps)
    assert "manager:fixture" in json.dumps(plan.model_dump(mode="json"))


@pytest.mark.parametrize("scope", ["국내", "해외", "국내/해외", "KODEX", "TIGER", "iShares", "미국 증시에 상장된 주식형"])
def test_existing_product_scope_prefix_is_never_a_management_company(scope):
    from app.query.analyzer import RuleBasedQueryAnalyzer
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(f"{scope} ETF 중 AUM이 높은 상위 3개"))
    assert not parsed.entities
    assert not parsed.relations


@pytest.mark.parametrize("company", ["캠브리콘", "기존에없는테스트회사"])
def test_holding_ranking_and_weight_projection_reach_entity_not_found(company):
    question = f"{company}을 보유한 ETF 중 최근 6개월 수익률 상위 3개를 찾고 각각의 편입 비중도 알려줘"
    llm = _FailingSemanticParserLLM()
    coordinator = _semantic_coordinator(llm)
    parsed = asyncio.run(coordinator.analyze(question))
    assert parsed.entities[0].raw_text == company
    assert parsed.relations[0].target_type == "Organization"
    assert parsed.metrics[0].temporal.period == "6M"
    assert parsed.sort[0].direction == "desc" and parsed.result_limit.value == 3
    weight = next(item for item in parsed.semantic_constraints if item.payload.get("property") == "weight")
    assert weight.payload["projection_scope"] == "path" and weight.payload["relation"] == "holds"
    assert parsed.requested_fields == ["편입 비중"]
    service = create_pipeline_answer_service()
    service._query_analyzer = coordinator
    service._entity_resolver = RegistryEntityResolver(StaticEntityLookup([]))
    service._ontology_service = _ontology()
    service._planner = _planner()
    result = asyncio.run(service.answer(question))
    assert "ENTITY_NOT_FOUND" in result.retrieved_context
    assert "SEMANTIC_PARSE_FAILED" not in result.retrieved_context
    assert llm.calls == 0


@pytest.mark.parametrize("question,reason", [
    ("최근 6개월 동안 AUM이 가장 많이 증가한 ETF", "historical_series_unavailable"),
    ("최근 1년 동안 순자산이 가장 크게 증가한 ETF", "historical_series_unavailable"),
    ("미래에셋 ETF 중 운용보수가 0.5% 이하인 상품", "expense_ratio_scale_unverified"),
    ("삼성 ETF 중 총보수가 0.3% 미만인 상품", "expense_ratio_scale_unverified"),
])
def test_understood_unsupported_conditions_reach_capability_validator(question, reason):
    async def run():
        llm = _FailingSemanticParserLLM()
        parsed = await _semantic_coordinator(llm).analyze(question)
        assert llm.calls == 0 and not parsed.unparsed_material_spans
        if reason.startswith("historical"):
            assert parsed.metrics[0].temporal.operation == "CHANGE"
        else:
            assert parsed.filters[0].value.raw in {"0.5%", "0.3%"}
            assert parsed.filters[0].operator.value in {"lte", "lt"}
        resolved = await RegistryEntityResolver(StaticEntityLookup()).resolve(parsed)
        grounded = await _ontology().ground(resolved)
        with pytest.raises(UnsupportedQuerySemanticsError) as caught:
            await _planner().create_plan(grounded)
        assert any(reason in value for value in caught.value.reasons)
    asyncio.run(run())


def test_unknown_residual_cannot_use_understood_unsupported_shortcut():
    llm = _FailingSemanticParserLLM()
    from app.query.exceptions import SemanticParseSafetyError
    with pytest.raises(SemanticParseSafetyError):
        asyncio.run(_semantic_coordinator(llm).analyze(
            "최근 6개월 동안 AUM이 가장 많이 증가한 ETF 특수미지조건",
        ))
    assert llm.calls == 1


def test_production_formatter_emits_only_sanitized_nested_diagnostic_fields():
    record = logging.LogRecord("app.query.llm_parser", logging.ERROR, "", 0, "invalid", (), None)
    record.validation_errors = [{
        "loc": ["filters", 0], "type": "value_error", "msg": "API_key=nv-secret",
        "input": "raw response", "ctx": {"Authorization": "Bearer secret"},
    }]
    record.parsed_top_level_keys = ["intent", "nv-secret"]
    record.raw_response = "raw response"
    rendered = JsonLogFormatter().format(record)
    assert "nv-secret" not in rendered and "raw response" not in rendered and "Bearer secret" not in rendered
    payload = json.loads(rendered)
    assert set(payload["validation_errors"][0]) == {"loc", "type", "msg"}
    assert payload["parsed_top_level_keys"] == ["intent", "[REDACTED]"]
