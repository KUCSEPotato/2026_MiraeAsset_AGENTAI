import asyncio
import json
import logging
from io import StringIO

import pytest
from fastapi.testclient import TestClient

from app.agent.service import get_answer_service
from app.evidence.llm_answer import AnswerGenerationError
from app.domain.models import AnswerabilityReasonCode
from app.main import create_app
from app.operations import JsonLogFormatter, OperationalSettings
from app.schemas.agent import AgentResult


class _Service:
    def __init__(
        self,
        *,
        delay: float = 0,
        error: Exception | None = None,
        result: AgentResult | None = None,
    ) -> None:
        self.delay = delay
        self.error = error
        self.result = result

    async def validate_derived_stores(self) -> None:
        return None

    def runtime_health(self) -> dict[str, str]:
        return {
            "active_runtime_bundle": "canonical_v2",
            "generation": "260824",
            "compatibility_status": "READY",
        }

    async def close(self) -> None:
        return None

    async def answer(self, question: str) -> AgentResult:
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result or AgentResult(
            retrieved_context="{}",
            think_trace=json.dumps(
                {
                    "status": "success",
                    "planner": "rule",
                    "planning_summary": {"sources": ["rdb"]},
                    "validation_summary": {"reason_codes": []},
                }
            ),
            answer="ok",
        )


def _client(monkeypatch, service: _Service) -> TestClient:
    monkeypatch.setattr("app.main.get_answer_service", lambda: service)
    application = create_app()
    application.dependency_overrides[get_answer_service] = lambda: service
    return TestClient(application)


_ANSWER_KEYS = {
    "question_id",
    "question",
    "retrieved_context",
    "think_trace",
    "answer",
}


def _assert_evaluation_envelope(
    response,
    *,
    question_id: str,
    question: str,
) -> dict[str, str]:
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == _ANSWER_KEYS
    assert all(isinstance(value, str) for value in payload.values())
    assert payload["question_id"] == question_id
    assert payload["question"] == question
    return payload


def _semantic_result(*, status: str, reason: str) -> AgentResult:
    return AgentResult(
        retrieved_context="",
        think_trace=json.dumps(
            {
                "status": status,
                "reason": reason,
                "validation_summary": {"reason_codes": [reason]},
            },
            ensure_ascii=False,
        ),
        answer="제공된 데이터에서 해당 정보를 확인할 수 없습니다.",
    )


def test_liveness_readiness_and_five_string_answer_contract(monkeypatch) -> None:
    with _client(monkeypatch, _Service()) as client:
        assert client.get("/live").json() == {"status": "alive"}
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["readiness_status"] == "READY"
        response = client.get(
            "/answer", params={"question_id": "Q-한글", "question": "미국 ETF?"}
        )
    _assert_evaluation_envelope(
        response,
        question_id="Q-한글",
        question="미국 ETF?",
    )


def test_request_timeout_uses_safe_evaluation_envelope(monkeypatch) -> None:
    with _client(monkeypatch, _Service(delay=0.05)) as client:
        client.app.state.operational_settings = OperationalSettings(
            request_timeout_seconds=0.01
        )
        response = client.get(
            "/answer", params={"question_id": "Q-timeout", "question": "질문"}
        )
    payload = _assert_evaluation_envelope(
        response,
        question_id="Q-timeout",
        question="질문",
    )
    assert json.loads(payload["think_trace"]) == {
        "steps": ["api_boundary"],
        "status": "internal_failure",
        "reason": "request_timeout",
    }


def test_answer_dependency_failure_uses_safe_evaluation_envelope(monkeypatch) -> None:
    service = _Service(error=AnswerGenerationError("upstream body must stay private"))
    with _client(monkeypatch, service) as client:
        response = client.get(
            "/answer", params={"question_id": "Q-upstream", "question": "질문"}
        )
    payload = _assert_evaluation_envelope(
        response,
        question_id="Q-upstream",
        question="질문",
    )
    assert json.loads(payload["think_trace"])["reason"] == (
        "answer_generation_dependency_failure"
    )
    assert "upstream body" not in response.text


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unsupported", "UNSUPPORTED_CONSTRAINT"),
        ("unanswerable", "ENTITY_NOT_FOUND"),
        ("unanswerable", "ZERO_MATCH"),
        ("parser_failure", "SEMANTIC_PARSE_FAILED"),
        ("unanswerable", "INSUFFICIENT_EVIDENCE"),
    ],
)
def test_semantic_failures_preserve_status_in_200_envelope(
    monkeypatch,
    status: str,
    reason: str,
) -> None:
    question_id = f"Q-{reason}"
    question = "평가 질의"
    service = _Service(result=_semantic_result(status=status, reason=reason))
    with _client(monkeypatch, service) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": question},
        )
    payload = _assert_evaluation_envelope(
        response,
        question_id=question_id,
        question=question,
    )
    trace = json.loads(payload["think_trace"])
    assert trace["status"] == status
    assert trace["reason"] == reason


def test_unexpected_service_failure_uses_sanitized_200_envelope(monkeypatch) -> None:
    secret = "postgresql://user:do-not-expose@example.test/database"
    with _client(monkeypatch, _Service(error=RuntimeError(secret))) as client:
        response = client.get(
            "/answer",
            params={"question_id": "Q-internal", "question": "질문"},
        )
    payload = _assert_evaluation_envelope(
        response,
        question_id="Q-internal",
        question="질문",
    )
    assert payload["retrieved_context"] == ""
    assert json.loads(payload["think_trace"]) == {
        "steps": ["api_boundary"],
        "status": "internal_failure",
        "reason": "safe_internal_error",
    }
    assert secret not in response.text


def test_unexpected_response_rendering_failure_uses_safe_200_envelope(
    monkeypatch,
) -> None:
    malformed = AgentResult(
        retrieved_context="{}",
        think_trace=json.dumps({"planning_summary": []}),
        answer="사용되면 안 되는 답변",
    )
    with _client(monkeypatch, _Service(result=malformed)) as client:
        response = client.get(
            "/answer",
            params={"question_id": "Q-render", "question": "질문"},
        )
    payload = _assert_evaluation_envelope(
        response,
        question_id="Q-render",
        question="질문",
    )
    assert payload["retrieved_context"] == ""
    assert json.loads(payload["think_trace"])["reason"] == "safe_internal_error"
    assert payload["answer"] == "요청 처리 중 일시적인 문제가 발생했습니다."


def test_unknown_query_parameter_is_ignored(monkeypatch) -> None:
    with _client(monkeypatch, _Service()) as client:
        response = client.get(
            "/answer",
            params={
                "question_id": "Q-extra",
                "question": "질문",
                "unknown_parameter": "ignored",
            },
        )
    _assert_evaluation_envelope(
        response,
        question_id="Q-extra",
        question="질문",
    )


def test_json_logging_redacts_connection_and_token_secrets() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("m10.9-redaction-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info(
        "DATABASE_URL=postgresql+psycopg://user:password@db/runtime "
        "Authorization=Bearer token-value api_key=secret-value"
    )
    rendered = stream.getvalue()
    assert "password@" not in rendered
    assert "token-value" not in rendered
    assert "secret-value" not in rendered
    assert rendered.count("[REDACTED]") == 3


def test_json_logging_emits_allowlisted_hyperclova_error_fields() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("hcx-safe-error-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.error(
        "HyperCLOVA request failed",
        extra={
            "http_status": 400,
            "hcx_error_code": "40055",
            "hcx_error_message": "Invalid response format schema",
            "request_purpose": "semantic_parse",
            "request_id": "request-safe-id",
        },
    )

    payload = json.loads(stream.getvalue())
    assert payload["http_status"] == 400
    assert payload["hcx_error_code"] == "40055"
    assert payload["hcx_error_message"] == "Invalid response format schema"
    assert payload["request_purpose"] == "semantic_parse"
    assert payload["request_id"] == "request-safe-id"


def test_app_timeout_must_leave_evaluator_margin(monkeypatch) -> None:
    monkeypatch.setenv("APP_TIMEOUT_SECONDS", "300")
    with pytest.raises(ValueError, match="below 300"):
        OperationalSettings.from_env()


def test_frontend_origin_is_allowed_for_browser_calls(monkeypatch) -> None:
    monkeypatch.setenv("FRONTEND_ORIGINS", "http://localhost:3000")
    with _client(monkeypatch, _Service()) as client:
        response = client.options(
            "/answer?question_id=Q-cors&question=ETF",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_evaluator_reason_code_uses_required_entity_ambiguous_name() -> None:
    assert AnswerabilityReasonCode.AMBIGUOUS_ENTITY.value == "ENTITY_AMBIGUOUS"
    assert AnswerabilityReasonCode.ENTITY_AMBIGUOUS.value == "ENTITY_AMBIGUOUS"
