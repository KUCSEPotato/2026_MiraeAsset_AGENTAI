from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.data.metric_capabilities import PREF01_RETURN_CONTRACTS
from app.domain.models import (
    FilterSpec, GroupBySpec, ParsedQuery, QueryIntent, TemporalSpec,
)
from app.entity.lookup import StaticEntityLookup, _default_entities
from app.entity.resolver import RegistryEntityResolver
from app.ontology.vocabulary import DEFAULT_SEMANTIC_VOCABULARY
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.exceptions import SemanticCandidateValidationError
from app.query.llm_parser import hyperclova_candidate_schema
from app.query.normalization import metric_spec, temporal_spec
from app.query.semantic_models import LLMSemanticParseCandidate
from app.query.semantic_validation import LLMSemanticCandidateValidator


def _parse(question: str) -> ParsedQuery:
    return asyncio.run(RuleBasedQueryAnalyzer().analyze(question))


def test_old_query_payload_remains_valid_and_new_operators_are_strict() -> None:
    parsed = ParsedQuery(original_question="ETF", intent="search_product")
    assert parsed.metrics == [] and parsed.comparison is None and parsed.group_by is None
    with pytest.raises(ValidationError):
        TemporalSpec(period="2Y")
    with pytest.raises(ValidationError):
        TemporalSpec(period="6M", sql="SELECT 1")
    with pytest.raises(ValidationError):
        GroupBySpec(fields=[])


@pytest.mark.parametrize("field,contract", PREF01_RETURN_CONTRACTS.items())
def test_metric_period_binding_uses_reviewed_contracts(field, contract) -> None:
    parsed = _parse(f"국내 ETF 중 {contract.exact_period} 수익률이 높은 상위 3개")
    assert parsed.semantic_coverage == "complete"
    assert len(parsed.metrics) == 1
    metric = parsed.metrics[0]
    assert metric.canonical_field == field
    assert metric.temporal.period == contract.exact_period
    assert metric.temporal.period_source == "EXPLICIT_QUERY"
    assert metric.constraint_id == parsed.sort[0].constraint_id


@pytest.mark.parametrize("phrase,period", [
    ("오늘", "1D"), ("1일", "1D"), ("최근 1개월", "1M"),
    ("최근 3개월", "3M"), ("최근 6개월", "6M"), ("최근 1년", "1Y"),
    ("올해", "YTD"), ("YTD", "YTD"),
])
def test_period_resolution_is_reusable_without_a_return_metric(phrase, period) -> None:
    assert temporal_spec(phrase).period == period


def test_explicit_unknown_period_never_becomes_default_return() -> None:
    assert metric_spec("수익률").temporal.period_source == "DEFAULT_POLICY"
    unknown = metric_spec("수익률", context="2년 수익률")
    assert unknown.temporal is None and unknown.canonical_field is None


@pytest.mark.parametrize("conjunction", ["와 ", ", ", " 및 "])
def test_multi_entity_multi_field_comparison_reuses_fixture_resolution(conjunction) -> None:
    products = [item for item in _default_entities() if item.entity_type == "product"]
    names = [item.official_name for item in products[:2]]
    parsed = _parse(f"{conjunction.join(names)}의 수익률과 AUM을 비교해줘")
    assert parsed.semantic_coverage == "complete"
    assert [item.raw_text for item in parsed.entities] == names
    assert parsed.comparison.fields == ["수익률", "AUM"]
    resolved = asyncio.run(RegistryEntityResolver(StaticEntityLookup()).resolve(parsed))
    assert {item.canonical_id for item in resolved.resolved_entities} == {
        item.canonical_id for item in products[:2]
    }
    assert {item.metric for item in parsed.metrics} == {"RETURN", "AUM"}


def test_comparison_without_fields_remains_unsupported() -> None:
    parsed = _parse("ETF를 비교해줘")
    assert parsed.comparison is None
    assert any(item.unsupported_reason == "true_ambiguity:comparison_metric_missing"
               for item in parsed.semantic_constraints)


def test_historical_growth_and_grouping_are_preserved_and_fail_closed() -> None:
    parsed = _parse("최근 6개월 동안 AUM이 가장 많이 증가한 ETF")
    assert parsed.metrics[0].temporal.operation == "CHANGE"
    assert any(item.unsupported_reason == "historical_metric_series_unavailable"
               for item in parsed.semantic_constraints)
    grouped = _parse("운용사별 ETF 개수")
    assert grouped.group_by.fields == ["운용사"]
    assert grouped.unsupported_constraint_ids


@pytest.mark.parametrize("question", [
    "매매단가와 수익률이 제공된 장외채권",
    "추가매수 가능한 펀드 중 최신 기준가가 있는 상품",
])
def test_operational_conditions_do_not_invent_metric_projections(question) -> None:
    parsed = _parse(question)
    assert parsed.metrics == []
    assert parsed.requested_fields == []


@pytest.mark.parametrize("value", [[], ["ETF"], 42, True, ""])
def test_contains_requires_one_nonempty_text_operand(value) -> None:
    with pytest.raises(ValidationError):
        FilterSpec(field="product.name", operator="contains", value=value)


def _span(question: str, value: str) -> dict:
    start = question.index(value)
    return {"start": start, "end": start + len(value), "raw_text": value}


def _validate(question: str, candidate: dict) -> ParsedQuery:
    return LLMSemanticCandidateValidator(DEFAULT_SEMANTIC_VOCABULARY).validate(
        question, _parse(question), LLMSemanticParseCandidate.model_validate(candidate),
        model="fixture", rule_latency_ms=0, llm_latency_ms=0,
        prompt_version="test", schema_version="test",
    )


def test_llm_structured_or_references_every_filter_leaf() -> None:
    question = "미국 또는 일본 ETF"
    spans = [_span(question, value) for value in ("미국", "일본")]
    parsed = _validate(question, {
        "intent": "search_product",
        "product_types": [{"source_span": _span(question, "ETF"), "value": "ETF"}],
        "filters": [{"source_span": span, "field": "region", "operator": "eq", "value": span["raw_text"]}
                    for span in spans],
        "boolean_expression": {"node_type": "or", "children": [
            {"node_type": "predicate", "predicate_span": span} for span in spans
        ]},
    })
    assert parsed.unsupported_constraint_ids == []
    assert parsed.boolean_expression.node_type == "or"
    assert {child.constraint_id for child in parsed.boolean_expression.children} == {
        item.constraint_id for item in parsed.filters
    }


def test_llm_unknown_field_operator_and_grouping_reject_or_fail_closed() -> None:
    question = "미국 ETF"
    with pytest.raises(SemanticCandidateValidationError):
        _validate(question, {"intent": "search_product", "requested_fields": [
            {"source_span": _span(question, "미국"), "value": "unknown.field"}
        ]})
    with pytest.raises(ValidationError):
        LLMSemanticParseCandidate.model_validate({"intent": "search_product", "filters": [
            {"source_span": _span(question, "미국"), "field": "region", "operator": "execute", "value": "미국"}
        ]})
    schema = hyperclova_candidate_schema()["properties"]
    assert "contains" in schema["filters"]["items"]["properties"]["operator"]["enum"]
    assert "group_by" in schema


def test_llm_group_by_keeps_unsupported_material_in_the_ir() -> None:
    question = "지역별 ETF"
    vocabulary = {key: list(values) for key, values in DEFAULT_SEMANTIC_VOCABULARY.items()}
    vocabulary["fields"].append("지역")
    candidate = LLMSemanticParseCandidate.model_validate({
        "intent": "search_product",
        "product_types": [{"source_span": _span(question, "ETF"), "value": "ETF"}],
        "group_by": [{"source_span": _span(question, "지역별"), "value": "지역"}],
    })
    parsed = LLMSemanticCandidateValidator(vocabulary).validate(
        question, _parse(question), candidate, model="fixture", rule_latency_ms=0,
        llm_latency_ms=0, prompt_version="test", schema_version="test",
    )
    assert parsed.group_by.fields == ["지역"]
    assert any(item.unsupported_reason == "group_by_execution_unsupported"
               for item in parsed.semantic_constraints)


def test_negated_filter_is_a_predicate_not_double_negation() -> None:
    parsed = _parse("미국을 제외한 ETF")
    negative = next(item for item in parsed.filters if item.operator == "ne")
    assert any(child.constraint_id == negative.constraint_id
               for child in parsed.boolean_expression.children)
