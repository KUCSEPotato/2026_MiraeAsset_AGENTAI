"""Clause-level facts never relax a filter, ranking, or comparison contract."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from app.agent.service import create_pipeline_answer_service
from app.data.metric_capabilities import PREF01_AUM, PREF01_ONE_YEAR_RETURN, PREF01_SIX_MONTH_RETURN
from app.domain.models import CanonicalEntity, RetrievalRecord, ValidationResult
from app.entity.lookup import StaticEntityLookup
from app.entity.normalization import normalized_entity_form, entity_lookup_keys, entity_name_similarity
from app.entity.resolver import RegistryEntityResolver
from app.evidence.answer import DeterministicEvidenceAnswerGenerator
from app.evidence.builder import GenericEvidenceBuilder
from app.evidence.quality import StaticFieldQualityProvider
from app.evidence.validator import QualityAwareEvidenceValidator
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.validator import StructuredQueryPlanValidator, QueryPlanValidationError
from tests.test_m10_5_semantic_safety import _TIGER_SP500, _tiger_plan
from tests.test_m10_9_c1_structured_operations import _ontology, _planner
from tests.test_m10_9_hyperclova_answer import _semantic_coordinator, _FailingSemanticParserLLM
from tests.test_semantic_composition_evidence import _query, _bundle, _validate


def _record(field, value, *, entity_id=_TIGER_SP500.canonical_id):
    contract = {item.canonical_field: item for item in (PREF01_AUM, PREF01_ONE_YEAR_RETURN, PREF01_SIX_MONTH_RETURN)}.get(field)
    return RetrievalRecord(source="rdb", source_id=f"fact:{entity_id}:{field}", entity_id=entity_id,
        payload={"field": field, "value": value}, metadata={
            "real_rdb": True, "dataset_snapshot": "2026-08-24", "observed_at": "2026-08-24",
            **({"comparison_contracts": [contract.as_plan_input()], "metric_dataset": contract.dataset,
                "metric_unit": contract.unit, "metric_scale_basis": contract.scale, "metric_currency": contract.currency,
                "field_fact_id": f"fact:{entity_id}:{field}", "field_evidence_assertion_ids": ["assertion:fixture"]}
               if contract else {}),
        })


def _answer(question, records, *, entities=None, answer_generator=None):
    calls = []
    class Executor:
        async def execute(self, plan):
            calls.append(plan)
            return records
    llm = _FailingSemanticParserLLM()
    service = create_pipeline_answer_service(executor=Executor())
    service._query_analyzer = _semantic_coordinator(llm)
    service._entity_resolver = RegistryEntityResolver(StaticEntityLookup(entities if entities is not None else [_TIGER_SP500]))
    service._ontology_service = _ontology()
    service._planner = _planner()
    service._evidence_builder = GenericEvidenceBuilder()
    service._evidence_validator = QualityAwareEvidenceValidator(StaticFieldQualityProvider())
    service._answer_generator = answer_generator or DeterministicEvidenceAnswerGenerator()
    result = asyncio.run(service.answer(question))
    return result, json.loads(result.think_trace), calls, llm


def test_legacy_bool_and_three_state_contract_are_consistent():
    assert ValidationResult(answerable=True).answerability == "FULLY_ANSWERABLE"
    assert ValidationResult(answerable=False).answerability == "UNANSWERABLE"
    with pytest.raises(ValidationError):
        ValidationResult(answerable=True, answerability="UNANSWERABLE")


def test_three_output_lookup_provides_two_facts_and_discloses_missing_risk():
    result, trace, calls, llm = _answer(
        "TIGER 미국S&P500 ETF의 AUM과 최근 6개월 수익률, 위험 정보를 알려줘",
        [_record("product.aum", "1000"), _record("product.six_month_return", "12.3")],
    )
    assert llm.calls == 0 and len(calls) == 1
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    assert "1000" in result.answer and "12.3%" in result.answer
    assert "위험등급: 현재 데이터에서 확인할 수 없음" in result.answer
    cells = trace["validation_summary"]["clauses"]
    assert [cell["status"] for cell in cells] == ["SATISFIED", "SATISFIED", "MISSING"]
    assert "PARTIALLY_ANSWERABLE" in result.retrieved_context


@pytest.mark.parametrize("outputs, expected_state, expected_cells", [
    ("AUM과 최근 6개월 수익률", "FULLY_ANSWERABLE", {
        "product.aum": "SATISFIED", "product.six_month_return": "SATISFIED",
    }),
    ("위험등급", "UNANSWERABLE", {"product.risk_grade": "MISSING"}),
    ("AUM과 최근 6개월 수익률과 위험등급", "PARTIALLY_ANSWERABLE", {
        "product.aum": "SATISFIED", "product.six_month_return": "SATISFIED", "product.risk_grade": "MISSING",
    }),
    ("AUM, 최근 6개월 수익률, 위험등급", "PARTIALLY_ANSWERABLE", {
        "product.aum": "SATISFIED", "product.six_month_return": "SATISFIED", "product.risk_grade": "MISSING",
    }),
])
def test_production_risk_projection_wording_preserves_verified_outputs(outputs, expected_state, expected_cells):
    class RejectGeneratedAnswer:
        async def generate(self, *args):
            raise AssertionError("missing/partial risk output must bypass the configured generator")

    question = f"TIGER 미국S&P500 ETF의 {outputs}을 알려줘"
    records = [] if outputs == "위험등급" else [
        _record("product.aum", "20158825743000"),
        _record("product.six_month_return", "6.46"),
    ]
    result, trace, calls, llm = _answer(
        question, records,
        answer_generator=RejectGeneratedAnswer() if expected_state != "FULLY_ANSWERABLE" else None,
    )
    assert len(calls) == 1 and llm.calls == 0
    step = calls[0].steps[0]
    assert step.source == "rdb"
    assert step.inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert set(step.inputs["requested_fields"]) == set(expected_cells)
    assert not step.inputs.get("sort") and not step.inputs.get("comparison")
    validation = trace["validation_summary"]
    assert validation["answerability"] == expected_state
    assert validation["answerable"] is (expected_state != "UNANSWERABLE")
    assert validation["comparison_completed"] is False
    cells = validation["clauses"]
    assert {cell["field"]: cell["status"] for cell in cells} == expected_cells
    for cell in cells:
        assert cell["entity_id"] == _TIGER_SP500.canonical_id
        if cell["status"] == "SATISFIED":
            assert cell["evidence_indices"]
            assert all(records[index].payload["field"] == cell["field"] for index in cell["evidence_indices"])
        else:
            assert cell["evidence_indices"] == []
    assert trace["llm_call_summary"]["semantic_parser_calls"] == 0
    if expected_state != "FULLY_ANSWERABLE":
        assert trace["llm_call_summary"]["answer_generation_calls"] == 0
    if records:
        assert "20158825743000" in result.answer and "6.46%" in result.answer
    if expected_state == "PARTIALLY_ANSWERABLE":
        assert "확인 가능한 정보" in result.answer
        assert "위험등급: 현재 데이터에서 확인할 수 없음" in result.answer
    assert all(text not in result.answer for text in ["RiskGrade.2", "중간 위험", "낮은 위험", "높은 위험", "1~5", "1~6"])


def test_risk_grade_literal_is_an_output_not_an_implicit_graph_relation():
    from app.planning.output_requirements import prepare_outputs
    parsed, _, grounded, plan = asyncio.run(_tiger_plan(
        "TIGER 미국S&P500 ETF의 AUM, 최근 6개월 수익률, 위험등급을 알려줘",
    ))
    assert parsed.requested_fields == ["AUM", "6개월 수익률", "위험등급"]
    assert not parsed.relations and not grounded.grounded_relations
    assert not parsed.filters and not parsed.sort
    before = grounded.model_dump(mode="json")
    prepared = prepare_outputs(grounded)
    StructuredQueryPlanValidator(RoutingMetadataRegistry()).validate(plan, grounded)
    assert grounded.model_dump(mode="json") == before
    assert prepared.query is not grounded and not prepared.disclosures
    assert not plan.output_disclosures  # Absence of a value is decided by evidence after execution.
    plan.steps[0].inputs["requested_fields"].remove("product.risk_grade")
    with pytest.raises(QueryPlanValidationError):
        StructuredQueryPlanValidator(RoutingMetadataRegistry()).validate(plan, grounded)


def test_risk_grade_literal_with_a_value_remains_value_only():
    result, trace, calls, _ = _answer("TIGER 미국S&P500 ETF의 위험등급을 알려줘",
                                    [_record("product.risk_grade", "RiskGrade.2")])
    assert len(calls) == 1
    assert trace["validation_summary"]["answerability"] == "FULLY_ANSWERABLE"
    assert "RiskGrade.2" in result.answer
    assert all(text not in result.answer for text in ["중간 위험", "낮은 위험", "높은 위험", "1~5", "1~6"])


@pytest.mark.parametrize("question", [
    "위험등급 1등급인 ETF를 알려줘",
    "국내 ETF 중 위험등급이 낮은 상위 3개를 알려줘",
    "TIGER 미국S&P500 ETF의 위험등급과 비밀지표를 알려줘",
])
def test_risk_projection_fix_cannot_drop_selection_or_unknown_material(question):
    result, trace, calls, _ = _answer(question, [_record("product.risk_grade", "RiskGrade.2")])
    assert not calls and "RiskGrade.2" not in result.answer
    assert trace["validation_summary"]["answerability"] == "UNANSWERABLE"


def test_partial_risk_projection_keeps_five_string_api_response():
    from fastapi.testclient import TestClient
    from app.agent.service import get_answer_service
    from app.main import create_app

    question = "TIGER 미국S&P500 ETF의 AUM, 최근 6개월 수익률, 위험등급을 알려줘"
    result, trace, _, _ = _answer(question, [
        _record("product.aum", "20158825743000"), _record("product.six_month_return", "6.46"),
    ])
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    class CompletedPipeline:
        async def answer(self, question):
            return result

    application = create_app()
    application.dependency_overrides[get_answer_service] = CompletedPipeline
    with TestClient(application) as client:
        response = client.get("/answer", params={"question_id": "risk-partial", "question": question})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"question_id", "question", "retrieved_context", "think_trace", "answer"}
    assert all(isinstance(value, str) for value in body.values())
    assert body["answer"] == result.answer


@pytest.mark.parametrize("missing", ["XYZ ETF", "다른 미국 S&P500 ETF"])
def test_partial_comparison_never_invents_entity_or_winner(missing):
    result, trace, calls, llm = _answer(
        f"TIGER 미국S&P500과 {missing}의 AUM과 수익률 비교",
        [_record("product.aum", "1000"), _record("product.one_year_return", "12.3")],
    )
    assert llm.calls == 0 and len(calls) == 1
    assert calls[0].steps[0].inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert calls[0].steps[0].inputs["comparison"] is None
    assert missing in result.answer and "1000" in result.answer and "12.3" in result.answer
    assert "비교는 완료하지 못" in result.answer
    assert trace["validation_summary"]["comparison_completed"] is False
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    assert trace["llm_call_summary"]["answer_generation_calls"] == 0
    if missing.startswith("다른"):
        assert any(item["reason"] == "peer_selector_unverified"
                   for item in trace["validation_summary"]["clauses"])


def test_peer_is_a_selector_and_never_reaches_entity_lookup():
    _, resolved, grounded, plan = asyncio.run(_tiger_plan(
        "TIGER 미국S&P500과 다른 미국 S&P500 ETF의 AUM과 수익률 비교",
    ))
    assert len(resolved.resolved_entities) == 1
    assert grounded.parsed_query.selectors[0].role == "PEER_SELECTOR"
    assert not grounded.grounded_filters  # peer exposure never filters anchor facts
    assert any(item.kind == "SELECTOR" for item in plan.output_disclosures)


def test_deferred_output_disclosure_cannot_be_removed_from_plan():
    _, _, grounded, plan = asyncio.run(_tiger_plan(
        "TIGER 미국S&P500과 XYZ ETF의 AUM과 수익률 비교",
    ))
    plan.output_disclosures = []
    with pytest.raises(QueryPlanValidationError, match="changed_output_disclosures"):
        StructuredQueryPlanValidator(RoutingMetadataRegistry()).validate(plan, grounded)


@pytest.mark.parametrize("question", [
    "운용보수가 0.5% 이하인 국내 ETF 중 최근 6개월 수익률 상위 3개를 찾고 각 상품의 AUM도 알려줘",
    "최근 6개월 동안 AUM이 가장 많이 증가한 ETF",
    "위험이 낮은 채권형 상품을 비교해줘",
    "위험등급 1등급인 ETF",
])
def test_hard_unsupported_constraints_never_execute_or_return_supplied_facts(question):
    result, trace, calls, _ = _answer(question, [_record("product.aum", "987654321")])
    assert not calls
    assert "987654321" not in result.answer
    assert trace["status"] != "success"


def test_all_missing_entities_never_broaden_into_unrestricted_search():
    result, _, calls, _ = _answer("ABC ETF와 XYZ ETF의 AUM과 수익률 비교", [], entities=[])
    assert not calls and "ENTITY_NOT_FOUND" in result.retrieved_context


def test_rejected_hard_filter_preserves_unexecuted_output_status():
    result, trace, calls, _ = _answer(
        "운용보수가 0.5% 이하인 국내 ETF 중 최근 6개월 수익률 상위 3개를 찾고 각 상품의 AUM도 알려줘",
        [_record("product.aum", "987654321")],
    )
    assert not calls and "987654321" not in result.answer
    validation = trace["validation_summary"]
    assert validation["answerability"] == "UNANSWERABLE"
    assert any(item["field"] == "product.aum" and item["status"] == "UNSUPPORTED"
               and item["reason"] == "query_not_executable" for item in validation["clauses"])
    assert json.loads(result.retrieved_context)["validation"]["clauses"] == validation["clauses"]


def test_all_outputs_missing_remain_unanswerable():
    _, trace, _, _ = _answer("TIGER 미국S&P500 ETF의 AUM과 위험 정보를 알려줘", [])
    assert trace["validation_summary"]["answerability"] == "UNANSWERABLE"


def test_ranking_clause_survives_missing_optional_projection():
    from app.data.metric_capabilities import PREF01_THREE_MONTH_RETURN
    ranking = _record("product.three_month_return", "12.3")
    ranking.metadata["comparison_contracts"] = [PREF01_THREE_MONTH_RETURN.as_plan_input()]
    ranking.metadata["metric_unit"] = "PERCENT"
    result, trace, calls, llm = _answer(
        "국내 ETF 중 최근 3개월 수익률 상위 5개를 찾고 각 상품의 AUM도 알려줘", [ranking],
    )
    assert llm.calls == 0 and len(calls) == 1
    assert calls[0].steps[0].inputs["top_n"] == {"value": 5}
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    assert "12.3%" in result.answer and "순자산: 현재 데이터에서 확인할 수 없음" in result.answer


def test_missing_ranking_metric_is_hard_even_when_aum_is_available():
    result, trace, _, _ = _answer(
        "국내 ETF 중 최근 3개월 수익률 상위 5개를 찾고 각 상품의 AUM도 알려줘",
        [_record("product.aum", "987654321")],
    )
    assert trace["validation_summary"]["answerability"] == "UNANSWERABLE"
    assert "987654321" not in result.answer


def test_unverified_risk_comparison_can_only_return_raw_facts():
    result, trace, calls, _ = _answer("TIGER 미국S&P500과 XYZ ETF의 위험 정보 비교",
                                    [_record("product.risk_grade", "RiskGrade.2")])
    assert calls[0].steps[0].inputs["comparison"] is None
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    assert not trace["validation_summary"]["comparison_completed"]
    assert "RiskGrade.2" in result.answer
    assert all(text not in result.answer for text in ["중간 위험", "낮은 위험", "높은 위험", "1~5", "1~6"])


def test_unsupported_output_is_disclosed_and_cannot_erase_supported_aum():
    result, trace, calls, _ = _answer("TIGER 미국S&P500 ETF의 AUM과 편입 비중도 알려줘",
                                    [_record("product.aum", "1000")])
    assert calls[0].steps[0].inputs["requested_fields"] == ["product.aum"]
    assert trace["validation_summary"]["answerability"] == "PARTIALLY_ANSWERABLE"
    assert "1000" in result.answer and "편입 비중" in result.answer


def test_ambiguous_comparison_member_is_disclosed_without_candidate_selection():
    from app.entity.lookup import _default_entities
    result, trace, calls, _ = _answer("TIGER 미국S&P500과 공통ETF의 AUM 비교",
                                    [_record("product.aum", "1000")], entities=[_TIGER_SP500, *_default_entities()])
    assert calls[0].steps[0].inputs["entity_ids"] == [_TIGER_SP500.canonical_id]
    assert any(item["status"] == "AMBIGUOUS" and item["kind"] == "ENTITY"
               for item in trace["validation_summary"]["clauses"])
    assert "하나로 특정할 수 없음" in result.answer


def test_complete_comparison_and_no_evidence_comparison_have_distinct_states():
    complete = _validate(_query(), _bundle())
    assert complete.answerability == "FULLY_ANSWERABLE" and complete.comparison_completed
    empty = _bundle().model_copy(update={"evidence": []})
    result = _validate(_query(), empty)
    assert result.answerability == "UNANSWERABLE" and not result.comparison_completed


def test_conflicting_cell_is_excluded_while_other_facts_survive():
    bundle = _bundle()
    duplicate = bundle.evidence[0].model_copy(update={"value": "99999", "source_id": "conflict"})
    bundle.evidence.append(duplicate)
    result = _validate(_query(), bundle)
    assert result.answerability == "PARTIALLY_ANSWERABLE" and not result.comparison_completed
    text = asyncio.run(DeterministicEvidenceAnswerGenerator().generate("compare", bundle, result))
    assert "99999" not in text
    assert any(item.status == "AMBIGUOUS" for item in result.clauses)


@pytest.mark.parametrize("suffix", ["증권", "자산운용", "은행", "보험"])
def test_organization_business_suffix_is_identity_not_disposable_context(suffix):
    qualified = f"테스트금융그룹{suffix}"
    root = "테스트금융그룹"
    assert normalized_entity_form(qualified, "organization") != normalized_entity_form(root, "organization")
    assert root not in entity_lookup_keys(qualified, "organization")
    assert entity_name_similarity(qualified, root, "organization") == 0
    wrong = CanonicalEntity(canonical_id="manager:wrong", entity_type="organization", official_name=root,
                            aliases=[qualified])
    assert not asyncio.run(StaticEntityLookup([wrong]).lookup(qualified, "organization"))


def test_legal_form_normalizes_but_securities_does_not_become_asset_manager():
    assert normalized_entity_form("미래에셋증권(주)", "organization") == normalized_entity_form("미래에셋증권", "organization")
    assert normalized_entity_form("미래에셋증권", "organization") != normalized_entity_form("미래에셋자산운용", "organization")


def test_partial_hcx_path_never_calls_model_or_paraphrases_missing_comparison():
    import httpx
    from app.evidence.llm_answer import HyperCLOVAEvidenceAnswerGenerator
    from tests.test_m10_9_hyperclova_answer import _settings
    bundle = _bundle()
    bundle.evidence.pop()
    validation = _validate(_query(), bundle)
    def reject(request):
        raise AssertionError("partial answer must not call HCX")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            return await HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client).generate(
                "누락값을 추정해서 비교해줘", bundle, validation,
            )
    text = asyncio.run(run())
    assert "비교는 완료하지 못" in text
    assert "현재 데이터에서 확인할 수 없음" in text


def test_missing_field_does_not_label_other_valid_evidence_as_invalid():
    from app.evidence.serializer import serialize_evidence_bundle
    bundle = _bundle()
    bundle.evidence.pop()
    text = serialize_evidence_bundle(bundle, _validate(_query(), bundle))
    assert "quality_status=invalid" not in text


def test_new_selector_and_clause_models_do_not_relax_llm_schema():
    from app.query.semantic_models import LLMSemanticParseCandidate
    with pytest.raises(ValidationError):
        LLMSemanticParseCandidate.model_validate({"intent": "compare_products", "selectors": []})
