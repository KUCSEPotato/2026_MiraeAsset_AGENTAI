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
    def __init__(self, *, delay: float = 0, error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error

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
        return AgentResult(
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


def test_liveness_readiness_and_five_string_answer_contract(monkeypatch) -> None:
    with _client(monkeypatch, _Service()) as client:
        assert client.get("/live").json() == {"status": "alive"}
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["readiness_status"] == "READY"
        response = client.get(
            "/answer", params={"question_id": "Q-한글", "question": "미국 ETF?"}
        )
    assert response.status_code == 200
    assert set(response.json()) == {
        "question_id", "question", "retrieved_context", "think_trace", "answer"
    }
    assert all(isinstance(value, str) for value in response.json().values())


def test_request_timeout_is_controlled_504(monkeypatch) -> None:
    with _client(monkeypatch, _Service(delay=0.05)) as client:
        client.app.state.operational_settings = OperationalSettings(
            request_timeout_seconds=0.01
        )
        response = client.get(
            "/answer", params={"question_id": "Q-timeout", "question": "질문"}
        )
    assert response.status_code == 504
    assert response.json() == {"detail": "answer request timed out"}


def test_answer_dependency_failure_is_controlled_503(monkeypatch) -> None:
    service = _Service(error=AnswerGenerationError("upstream body must stay private"))
    with _client(monkeypatch, service) as client:
        response = client.get(
            "/answer", params={"question_id": "Q-upstream", "question": "질문"}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "answer generation dependency unavailable"}
    assert "upstream body" not in response.text


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


def test_evaluator_reason_code_uses_required_entity_ambiguous_name() -> None:
    assert AnswerabilityReasonCode.AMBIGUOUS_ENTITY.value == "ENTITY_AMBIGUOUS"
    assert AnswerabilityReasonCode.ENTITY_AMBIGUOUS.value == "ENTITY_AMBIGUOUS"
