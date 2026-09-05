import asyncio
import json
import logging
from contextlib import contextmanager

import httpx
import pytest

from app.agent.service import create_pipeline_answer_service
from app.domain.models import Evidence, EvidenceBundle, ValidationResult
from app.evidence.llm_answer import (
    AnswerGenerationError,
    HyperCLOVAAnswerSettings,
    HyperCLOVAEvidenceAnswerGenerator,
)
from app.evidence.safe_response import ReasonAwareSafeResponseGenerator
from app.entity.exceptions import EntityResolutionDependencyError
from app.ontology.vocabulary import DEFAULT_SEMANTIC_VOCABULARY
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.config import HyperCLOVASemanticParserSettings
from app.query.exceptions import SemanticParseSafetyError, SemanticParserError
from app.query.llm_parser import (
    HyperCLOVASemanticParserClient,
    PROMPT_VERSION,
    SEMANTIC_SCHEMA_VERSION,
)
from app.query.semantic_models import SemanticParserRequest
from app.query.semantic_parser import SemanticParserCoordinator
from app.query.semantic_validation import LLMSemanticCandidateValidator


def _settings() -> HyperCLOVAAnswerSettings:
    return HyperCLOVAAnswerSettings(enabled=True, api_key="test-secret")


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        question="미국 ETF",
        evidence=[
            Evidence(
                source_type="rdb",
                source_id="fact-1",
                entity_id="ETF:1",
                field="product.name",
                value="Example ETF",
                dataset_snapshot="2026-08-24",
            )
        ],
    )


class _FailingSemanticParserLLM:
    model_name = "HCX-007"

    def __init__(self) -> None:
        self.calls = 0

    async def parse(self, request):
        del request
        self.calls += 1
        raise SemanticParserError(
            http_status=400,
            provider_code="40055",
        )


@contextmanager
def _capture_records(logger_name: str):
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(logger_name)
    handler = CaptureHandler()
    previous_level = target.level
    previous_disabled = target.disabled
    previous_global_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    target.disabled = False
    target.addHandler(handler)
    target.setLevel(logging.ERROR)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous_level)
        target.disabled = previous_disabled
        logging.disable(previous_global_disable)


def _semantic_coordinator(llm) -> SemanticParserCoordinator:
    return SemanticParserCoordinator(
        rule_parser=RuleBasedQueryAnalyzer(),
        llm_parser=llm,
        candidate_validator=LLMSemanticCandidateValidator(
            DEFAULT_SEMANTIC_VOCABULARY
        ),
        compact_vocabulary=DEFAULT_SEMANTIC_VOCABULARY,
    )


def test_hyperclova_answer_is_one_shot_and_evidence_bounded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"result": {"message": {"content": "검증된 답변"}}},
        )

    async def scenario() -> str:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        generator = HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client)
        try:
            return await generator.generate(
                "미국 ETF", _bundle(), ValidationResult(answerable=True)
            )
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == "검증된 답변"
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-secret"
    assert b"ETF:1" in requests[0].content
    payload = json.loads(requests[0].content)
    assert payload["topP"] == 0.2
    assert payload["maxCompletionTokens"] == 1_024
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]
    assert "PERCENT" in payload["messages"][0]["content"]
    assert "퍼센티지 포인트" in payload["messages"][0]["content"]


@pytest.mark.parametrize(
    ("question", "field", "limit"),
    [
        (
            "국내 ETF 중 최근 1년 수익률이 높은 상위 3개를 알려줘",
            "1년 수익률",
            3,
        ),
        (
            "국내 ETF 중 최근 6개월 수익률 상위 5개",
            "6개월 수익률",
            5,
        ),
        ("국내 ETF 중 올해 수익률 상위 3개", "올해 수익률", 3),
    ],
)
def test_natural_return_rankings_do_not_call_semantic_llm(
    question: str,
    field: str,
    limit: int,
) -> None:
    llm = _FailingSemanticParserLLM()
    parsed = asyncio.run(_semantic_coordinator(llm).analyze(question))

    assert llm.calls == 0
    assert parsed.parser_source.value == "rule"
    assert parsed.semantic_coverage.value == "complete"
    assert parsed.sort[0].field == field
    assert parsed.sort[0].direction == "desc"
    assert parsed.result_limit is not None
    assert parsed.result_limit.value == limit


def test_hyperclova_semantic_parse_payload_omits_unsupported_parameters() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "result": {
                    "message": {
                        "content": json.dumps({"intent": "search_product"})
                    }
                }
            },
        )

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        parser = HyperCLOVASemanticParserClient(
            HyperCLOVASemanticParserSettings(api_key="secret-test-key"),
            http_client=client,
        )
        try:
            return await parser.parse(SemanticParserRequest(
                original_question="반도체 ETF",
                rule_parse={},
                compact_vocabulary={},
                semantic_schema_version=SEMANTIC_SCHEMA_VERSION,
                prompt_version=PROMPT_VERSION,
            ))
        finally:
            await client.aclose()

    parsed = asyncio.run(scenario())
    payload = captured["payload"]

    assert parsed.intent.value == "search_product"
    assert isinstance(payload, dict)
    assert "thinking" not in payload
    assert "responseFormat" not in payload
    assert payload["temperature"] == 0.0
    assert [item["role"] for item in payload["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"status": "failed"}),
        httpx.Response(200, json={"unexpected": "shape"}),
    ],
)
def test_hyperclova_answer_failure_is_controlled(response: httpx.Response) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: response)
        client = httpx.AsyncClient(transport=transport)
        generator = HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client)
        try:
            with pytest.raises(AnswerGenerationError):
                await generator.generate(
                    "미국 ETF", _bundle(), ValidationResult(answerable=True)
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_hyperclova_answer_timeout_is_controlled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        generator = HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client)
        try:
            with pytest.raises(AnswerGenerationError, match="generation failed"):
                await generator.generate(
                    "미국 ETF", _bundle(), ValidationResult(answerable=True)
                )
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_hyperclova_answer_http_error_logs_safe_purpose() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": {
                    "code": "40001",
                    "message": "Invalid parameter api_key=must-not-leak",
                }
            },
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        generator = HyperCLOVAEvidenceAnswerGenerator(_settings(), http_client=client)
        try:
            with pytest.raises(AnswerGenerationError):
                await generator.generate(
                    "미국 ETF", _bundle(), ValidationResult(answerable=True)
                )
        finally:
            await client.aclose()

    with _capture_records("app.evidence.llm_answer") as records:
        asyncio.run(scenario())

    record = next(
        item for item in records
        if item.getMessage() == "HyperCLOVA request failed"
    )
    assert record.hcx_error_code == "40001"
    assert record.hcx_error_message == "Invalid parameter api_key=[REDACTED]"
    assert record.request_purpose == "answer_generation"
    assert record.request_id
    assert all("test-secret" not in item.getMessage() for item in records)


@pytest.mark.parametrize(
    ("question", "coverage", "unparsed", "descriptive"),
    [
        (
            "국민성장펀드의 구조와 투자전략 동향을 찾아서 알려줘",
            "complete",
            [],
            True,
        ),
        (
            "캠브리콘이 편입된 중국반도체 ETF를 알려줘",
            "incomplete",
            ["반도체"],
            False,
        ),
        (
            "최근 6개월동안 우주항공테마와 연관 이력이 있는 관련 ETF를 정리해줘",
            "complete",
            [],
            True,
        ),
        (
            "에코프로의 자회사를 편입한 ETF 중 순자산이 큰 상품의 위험요인 알려줘",
            "complete",
            [],
            True,
        ),
    ],
)
def test_false_negative_examples_reach_hcx_and_preserve_parser_failure(
    question: str,
    coverage: str,
    unparsed: list[str],
    descriptive: bool,
) -> None:
    rule = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))
    assert rule.semantic_coverage.value == coverage
    assert [item.raw_text for item in rule.unparsed_material_spans] == unparsed
    assert (
        rule.requires_semantic_search and bool(rule.semantic_terms)
    ) is descriptive

    llm = _FailingSemanticParserLLM()
    with pytest.raises(SemanticParseSafetyError) as caught:
        asyncio.run(_semantic_coordinator(llm).analyze(question))
    assert llm.calls == 1
    assert caught.value.reason == "semantic_parse_dependency_failure"


def test_hyperclova_semantic_400_logs_only_safe_diagnostics() -> None:
    question = "반도체 ETF"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": {
                    "code": "40055",
                    "message": (
                        "Invalid response format schema "
                        "Authorization=Bearer nv-must-not-leak"
                    ),
                }
            },
        )

    async def scenario() -> SemanticParserError:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        parser = HyperCLOVASemanticParserClient(
            HyperCLOVASemanticParserSettings(api_key="secret-test-key"),
            http_client=client,
        )
        try:
            with pytest.raises(SemanticParserError) as caught:
                await parser.parse(SemanticParserRequest(
                    original_question=question,
                    rule_parse={},
                    compact_vocabulary={},
                    semantic_schema_version=SEMANTIC_SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                ))
            return caught.value
        finally:
            await client.aclose()

    with _capture_records("app.query.llm_parser") as records:
        error = asyncio.run(scenario())

    assert error.http_status == 400
    assert error.provider_code == "40055"
    record = next(
        item for item in records
        if item.getMessage() == "HyperCLOVA request failed"
    )
    assert record.http_status == 400
    assert record.hcx_error_code == "40055"
    assert record.hcx_error_message == (
        "Invalid response format schema Authorization=Bearer [REDACTED]"
    )
    assert record.request_purpose == "semantic_parse"
    assert record.request_id
    assert all("secret-test-key" not in item.getMessage() for item in records)
    assert all(question not in item.getMessage() for item in records)


def test_semantic_parser_dependency_failure_is_not_unsupported_constraint() -> None:
    class FailingAnalyzer:
        async def analyze(self, question: str):
            del question
            raise SemanticParseSafetyError(
                "semantic_parse_dependency_failure"
            )

    class MustNotExecute:
        async def execute(self, plan):
            del plan
            raise AssertionError("parser failure reached execution")

    service = create_pipeline_answer_service(executor=MustNotExecute())
    service._query_analyzer = FailingAnalyzer()
    service._safe_response_generator = ReasonAwareSafeResponseGenerator()
    result = asyncio.run(service.answer("복합 금융 질의"))
    trace = json.loads(result.think_trace)

    assert trace["status"] == "parser_failure"
    assert trace["reason"] == "semantic_parse_failed"
    assert trace["validation_summary"]["reason_codes"] == [
        "SEMANTIC_PARSE_FAILED"
    ]
    assert "UNSUPPORTED_CONSTRAINT" not in result.retrieved_context
    assert "질의 해석 서비스를 완료하지 못해" in result.answer


def test_entity_repository_failure_is_not_entity_not_found() -> None:
    class FailingResolver:
        async def resolve(self, query):
            del query
            raise EntityResolutionDependencyError("database unavailable")

    service = create_pipeline_answer_service()
    service._entity_resolver = FailingResolver()
    service._safe_response_generator = ReasonAwareSafeResponseGenerator()
    result = asyncio.run(service.answer("테스트 ETF"))
    trace = json.loads(result.think_trace)

    assert trace["status"] == "entity_resolution_failure"
    assert trace["reason"] == "entity_resolution_failed"
    assert trace["validation_summary"]["reason_codes"] == [
        "ENTITY_RESOLUTION_FAILED"
    ]
    assert "ENTITY_NOT_FOUND" not in result.retrieved_context


def test_enabled_answer_generation_requires_credentials() -> None:
    with pytest.raises(ValueError, match="CLOVASTUDIO_API_KEY"):
        HyperCLOVAAnswerSettings(enabled=True).validate()
