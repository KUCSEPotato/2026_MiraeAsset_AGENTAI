from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.agent.exceptions import AgentUnavailableError
from app.agent.service import AnswerService, get_answer_service
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def assert_answer_contract(payload: dict[str, object]) -> None:
    assert set(payload) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }
    assert all(isinstance(value, str) for value in payload.values())


def test_answer_returns_exact_contract(client: TestClient) -> None:
    response = client.get(
        "/answer",
        params={"question_id": "Q-001", "question": "테스트 질문"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert_answer_contract(payload)
    assert payload["question_id"] == "Q-001"
    assert payload["question"] == "테스트 질문"


def test_answer_preserves_korean_question(client: TestClient) -> None:
    question = "국내 ETF 중 운용보수가 낮은 상품을 알려줘"

    response = client.get(
        "/answer",
        params={"question_id": "Q-한글", "question": question},
    )

    assert response.status_code == 200
    assert response.json()["question"] == question


@pytest.mark.parametrize(
    ("params", "expected_detail"),
    [
        ({"question_id": "Q-001", "question": ""}, None),
        ({"question_id": "Q-001", "question": "   "}, "question must not be blank"),
        ({"question_id": "", "question": "질문"}, None),
        ({"question_id": "   ", "question": "질문"}, "question_id must not be blank"),
    ],
)
def test_blank_parameters_return_422(
    client: TestClient,
    params: dict[str, str],
    expected_detail: str | None,
) -> None:
    response = client.get("/answer", params=params)

    assert response.status_code == 422
    if expected_detail is not None:
        assert response.json()["detail"] == expected_detail


def test_long_question_is_not_truncated(client: TestClient) -> None:
    # Keep the encoded GET query below HTTPX's URL safety limit while still
    # exercising a question much longer than normal evaluation input.
    question = "긴 금융 질문입니다. " * 500

    response = client.get(
        "/answer",
        params={"question_id": "Q-LONG", "question": question},
    )

    assert response.status_code == 200
    assert response.json()["question"] == question


def test_repeated_request_is_deterministic(client: TestClient) -> None:
    params = {"question_id": "Q-RETRY", "question": "동일 요청"}

    first = client.get("/answer", params=params)
    second = client.get("/answer", params=params)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_expected_agent_failure_returns_degraded_contract(client: TestClient) -> None:
    class UnavailableService:
        async def answer(self, question: str):
            raise AgentUnavailableError("retrieval timeout")

    def override_service() -> AnswerService:
        return UnavailableService()

    app.dependency_overrides[get_answer_service] = override_service

    response = client.get(
        "/answer",
        params={"question_id": "Q-FAIL", "question": "실패 테스트"},
    )

    assert response.status_code == 200
    assert_answer_contract(response.json())
    assert "근거" in response.json()["answer"]


def test_unexpected_agent_bug_is_not_hidden() -> None:
    class BrokenService:
        async def answer(self, question: str):
            raise RuntimeError("programming bug")

    def override_service() -> AnswerService:
        return BrokenService()

    app.dependency_overrides[get_answer_service] = override_service
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.get(
                "/answer",
                params={"question_id": "Q-BUG", "question": "버그 테스트"},
            )
        assert response.status_code == 500
    finally:
        app.dependency_overrides.clear()


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
