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


@pytest.mark.parametrize("question,limit", [
    ("채권 3개 보여줘.", 3), ("ETF 5개 보여줘.", 5), ("국내 ETF 5개 보여줘.", 5),
    ("국내 ETF 중 순자산이 큰 순서로 5개 보여줘.", 5),
    ("국내 ETF 중 AUM이 높은 순으로 7개 보여줘", 7),
    ("국내 ETF 순자산 내림차순으로 4개 보여줘", 4),
])
def test_listing_and_aum_order_phrases_reach_rdb_without_llm(question, limit):
    from app.domain.models import ResolvedQuery
    async def run():
        llm = _FailingSemanticParserLLM()
        parsed = await _semantic_coordinator(llm).analyze(question)
        plan = await _planner().create_plan(await _ontology().ground(ResolvedQuery(parsed_query=parsed)))
        return parsed, plan, llm
    parsed, plan, llm = asyncio.run(run())
    assert llm.calls == 0 and not parsed.unparsed_material_spans
    assert parsed.result_limit.value == limit
    assert len(plan.steps) == 1 and plan.steps[0].source == "rdb"
    if parsed.sort:
        assert parsed.sort[0].direction == "desc"
        assert plan.steps[0].inputs["sort"][0]["canonical_field"] == "product.aum"
        assert plan.steps[0].inputs["top_n"] == {"value": limit}
        assert plan.steps[0].inputs["product_universe"]["operands"] == ["DomesticETF"]


@pytest.mark.parametrize("question,year,direction", [
    ("만기가 2027년인 채권 5개 보여줘.", "2027", None),
    ("만기일이 2028년인 채권 5개 보여줘.", "2028", None),
    ("2029년에 만기가 도래하는 채권 5개 보여줘.", "2029", None),
    ("채권을 만기일이 빠른 순서로 5개 보여줘.", None, "asc"),
    ("채권 만기 오름차순으로 5개 보여줘.", None, "asc"),
    ("채권 만기일이 늦은 순으로 5개 보여줘.", None, "desc"),
])
def test_maturity_is_preserved_then_rejected_by_execution_capability(question, year, direction):
    from app.domain.models import ResolvedQuery
    async def run():
        llm = _FailingSemanticParserLLM()
        parsed = await _semantic_coordinator(llm).analyze(question)
        assert llm.calls == 0
        before = parsed.model_dump(mode="json")
        with pytest.raises(UnsupportedQuerySemanticsError):
            await _planner().create_plan(await _ontology().ground(ResolvedQuery(parsed_query=parsed)))
        assert parsed.model_dump(mode="json") == before
        return parsed
    parsed = asyncio.run(run())
    assert parsed.temporal_constraint is None and not parsed.unparsed_material_spans
    assert parsed.result_limit.value == 5
    if year:
        assert parsed.filters[0].operator == "between"
        assert parsed.filters[0].value == [f"{year}-01-01", f"{year}-12-31"]
        cell = next(c for c in parsed.semantic_constraints if c.constraint_id == parsed.filters[0].constraint_id)
        assert cell.payload["value"] == parsed.filters[0].value
        assert year in cell.raw_text
        assert question[cell.source_span.start:cell.source_span.end] == cell.raw_text
    else:
        assert parsed.sort[0].direction == direction


def _date_candidate(question):
    def span(raw):
        start = question.index(raw)
        return {"start": start, "end": start + len(raw), "raw_text": raw}
    return {"intent": "search_product", "product_types": [{"value": "채권", "source_span": span("채권")}],
            "filters": [{"field": "만기", "operator": "eq", "value": "2027년",
                         "source_span": span("만기가 2027년인")}],
            "result_limit": {"value": 5, "source_span": span("5개")}}


@pytest.fixture
def diagnostic_logging(monkeypatch):
    # Alembic's fileConfig in earlier tests disables existing app loggers.
    # Isolate the observable logging state and restore it after each test.
    previous_disable = logging.root.manager.disable
    loggers = [logging.getLogger(name) for name in (
        "app.query.semantic_parser", "app.query.llm_parser", "app.api.answer",
    )]
    previous_levels = [logger.level for logger in loggers]
    logging.disable(logging.NOTSET)
    for logger in loggers:
        monkeypatch.setattr(logger, "disabled", False)
        monkeypatch.setattr(logger, "propagate", True)
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, level in zip(loggers, previous_levels, strict=True):
            logger.setLevel(level)
        logging.disable(previous_disable)


@pytest.mark.parametrize("fault,expected", [
    (None, None), ("span", None), ("snapshot", "candidate_omits_rule_material"),
    ("date_literal", "candidate_value_not_grounded_in_span"),
    ("field", "unknown_filter_field"), ("sort_field", "unknown_sort_field"),
])
def test_real_llm_client_and_validator_with_mock_maturity_response(fault, expected, caplog, diagnostic_logging):
    from app.ontology.vocabulary import export_compact_semantic_vocabulary
    from app.query.analyzer import RuleBasedQueryAnalyzer
    from app.query.config import HyperCLOVASemanticParserSettings
    from app.query.exceptions import SemanticParseSafetyError
    from app.query.llm_parser import HyperCLOVASemanticParserClient
    from app.query.semantic_parser import SemanticParserCoordinator
    from app.query.semantic_validation import LLMSemanticCandidateValidator
    question = "만기가 2027년인 채권 5개 보여줘."
    candidate = _date_candidate(question)
    if fault == "span":
        candidate["filters"][0]["source_span"]["start"] += 1
    elif fault == "snapshot":
        candidate["temporal_condition"] = {"source_span": candidate["filters"].pop()["source_span"],
                                           "requested_snapshot": "2027-12-31"}
    elif fault == "date_literal":
        candidate["filters"][0].update(operator="between", value=["2027-01-01", "2027-12-31"])
    elif fault == "field":
        candidate["filters"][0]["field"] = "unverified_field"
    elif fault == "sort_field":
        candidate["sorts"] = [{"field": "unverified_sort", "direction": "asc",
                               "source_span": candidate["filters"][0]["source_span"]}]
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"result": {"message": {"content": json.dumps(candidate)}}})
    class ProbeRule(RuleBasedQueryAnalyzer):
        async def analyze(self, question):
            # Diagnostic only: force fallback while preserving every real rule clause.
            from app.domain.models import SemanticCoverageStatus
            parsed = await super().analyze(question)
            return parsed.model_copy(update={"semantic_coverage": SemanticCoverageStatus.INCOMPLETE})
    async def run():
        vocabulary = export_compact_semantic_vocabulary(_ontology().index)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = HyperCLOVASemanticParserClient(HyperCLOVASemanticParserSettings(api_key="nv-test-secret"), http_client=http)
            coordinator = SemanticParserCoordinator(rule_parser=ProbeRule(), llm_parser=client,
                candidate_validator=LLMSemanticCandidateValidator(vocabulary), compact_vocabulary=vocabulary)
            return await coordinator.analyze(question)
    if expected:
        with pytest.raises(SemanticParseSafetyError, match="semantic parsing"):
            asyncio.run(run())
        record = next(
            r for r in caplog.records
            if r.msg == "semantic parse candidate rejected"
        )
        diagnostic = json.loads(JsonLogFormatter().format(record))
        assert expected in diagnostic["candidate_rejection_reasons"]
        assert diagnostic["failure_stage"] == "candidate_validation"
        assert question not in json.dumps(diagnostic) and "nv-test-secret" not in json.dumps(diagnostic)
    else:
        parsed = asyncio.run(run())
        assert parsed.parser_source == "llm_fallback"
        assert parsed.filters[0].value == ["2027-01-01", "2027-12-31"]
        assert parsed.temporal_constraint is None
        assert all(question[c.source_span.start:c.source_span.end] == c.raw_text
                   for c in parsed.semantic_constraints)
    assert len(requests) == (2 if expected else 1)


@pytest.mark.parametrize("fault,reason,stage", [
    ("timeout", "semantic_parse_timeout", "timeout"),
    ("json", "semantic_parse_response_invalid", "response_json"),
    ("schema", "semantic_parse_response_invalid", "response_schema"),
])
def test_semantic_response_failure_stages_are_distinct(fault, reason, stage, caplog, diagnostic_logging):
    from app.query.config import HyperCLOVASemanticParserSettings
    from app.query.exceptions import SemanticParseSafetyError
    from app.query.llm_parser import HyperCLOVASemanticParserClient
    def handler(request):
        if fault == "timeout":
            raise httpx.ReadTimeout("secret raw content", request=request)
        return httpx.Response(200, json={"result": {"message": {
            "content": "secret raw content" if fault == "json" else '{"intent":"search_product","unknown":"secret raw content"}',
        }}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = HyperCLOVASemanticParserClient(HyperCLOVASemanticParserSettings(api_key="nv-test-secret"), http_client=http)
            return await _semantic_coordinator(client).analyze("특수미지조건 채권 5개 보여줘")
    with pytest.raises(SemanticParseSafetyError) as error:
        asyncio.run(run())
    assert error.value.reason == reason
    logs = [json.loads(JsonLogFormatter().format(r)) for r in caplog.records if r.name == "app.query.llm_parser"]
    assert any(row.get("failure_stage") == stage for row in logs)
    assert "secret raw content" not in json.dumps(logs) and "nv-test-secret" not in json.dumps(logs)


def test_api_correlates_concurrent_parser_and_empty_search_results_without_raw_logs(diagnostic_logging):
    from io import StringIO
    from app.agent.service import get_answer_service
    from app.domain.models import RetrievalResult, RetrievalSource
    from app.execution.config import ExecutionSettings
    from app.execution.executor import QueryExecutor
    from app.execution.transforms import InternalTransformExecutor
    from app.evidence.builder import GenericEvidenceBuilder
    from app.evidence.quality import StaticFieldQualityProvider
    from app.evidence.validator import QualityAwareEvidenceValidator
    from app.main import create_app
    from app.operations import request_correlation_id
    from app.query.config import HyperCLOVASemanticParserSettings
    from app.query.llm_parser import HyperCLOVASemanticParserClient
    from app.retrieval.registry import RetrieverRegistry

    calls = []
    class EmptyStore:
        async def retrieve_with_result(self, step, context):
            calls.append(step)
            await asyncio.sleep(0)
            return RetrievalResult(records=[], total_matches=0, returned_count=0)
    class RejectAnswer:
        async def generate(self, *args):
            raise AssertionError("unanswerable requests cannot fabricate model answers")
    def handler(request):
        return httpx.Response(200, json={"result": {"message": {
            "content": '{"intent":"search_product"}',
        }}})
    questions = ["채권 3개 보여줘.", "특수미지조건 채권 5개 보여줘.", "만기가 2027년인 채권 5개 보여줘."]
    sink = StringIO()
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            llm = HyperCLOVASemanticParserClient(HyperCLOVASemanticParserSettings(api_key="nv-test-secret"), http_client=http)
            executor = QueryExecutor(registry=RetrieverRegistry({RetrievalSource.RDB: EmptyStore()}),
                transform_executor=InternalTransformExecutor(), settings=ExecutionSettings())
            service = create_pipeline_answer_service(executor=executor)
            service._query_analyzer = _semantic_coordinator(llm)
            service._entity_resolver = RegistryEntityResolver(StaticEntityLookup([]))
            service._ontology_service = _ontology()
            service._planner = _planner()
            service._evidence_builder = GenericEvidenceBuilder()
            service._evidence_validator = QualityAwareEvidenceValidator(StaticFieldQualityProvider())
            service._answer_generator = RejectAnswer()
            application = create_app()
            application.dependency_overrides[get_answer_service] = lambda: service
            log_handler = logging.StreamHandler(sink)
            log_handler.setFormatter(JsonLogFormatter())
            logging.getLogger().addHandler(log_handler)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as api:
                    responses = await asyncio.gather(*[
                        api.get("/answer", params={"question_id": str(i), "question": question})
                        for i, question in enumerate(questions)
                    ])
                assert request_correlation_id.get() is None
                return responses
            finally:
                logging.getLogger().removeHandler(log_handler)
    responses = asyncio.run(run())
    assert len(calls) == 1
    identities = [response.headers["X-Request-ID"] for response in responses]
    assert len(set(identities)) == 3
    logs = [json.loads(line) for line in sink.getvalue().splitlines()]
    for index, response in enumerate(responses):
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"question_id", "question", "retrieved_context", "think_trace", "answer"}
        assert all(isinstance(value, str) for value in body.values())
        assert not json.loads(body["think_trace"])["validation_summary"]["answerable"]
        final = next(row for row in logs if row.get("question_id") == str(index))
        assert final["correlation_id"] == identities[index] == final["request_id"]
    assert "ZERO_MATCH" in json.loads(responses[0].json()["think_trace"])["validation_summary"]["reason_codes"]
    assert json.loads(responses[1].json()["think_trace"])["status"] == "parser_failure"
    assert json.loads(responses[2].json()["think_trace"])["status"] == "unsupported"
    rejected = next(row for row in logs if row.get("failure_stage") == "candidate_validation")
    received = next(row for row in logs if row.get("request_purpose") == "semantic_parse" and row.get("http_status") == 200)
    assert rejected["correlation_id"] == received["correlation_id"] == identities[1]
    # Only application logs: the test client's URL log includes its own query string.
    application_logs = json.dumps([row for row in logs if row["logger"].startswith("app.")])
    assert all(question not in application_logs for question in questions)
    assert "nv-test-secret" not in application_logs and '"content"' not in application_logs
