"""Verify the backend API after moving UI serving into the frontend container."""
from fastapi.testclient import TestClient

from app.agent.service import get_answer_service
from app.main import create_app
from app.schemas.agent import AgentResult


class AnswerStub:
    async def answer(self, question: str) -> AgentResult:
        return AgentResult(answer=f"응답: {question}", retrieved_context="{}", think_trace="{}")


def test_backend_serves_api_without_frontend():
    application = create_app()
    application.dependency_overrides[get_answer_service] = AnswerStub
    with TestClient(application) as client:
        # UI routes belong exclusively to the frontend container.
        for path in ("/", "/chat", "/assets/app.js"):
            assert client.get(path).status_code == 404
        response = client.get("/answer", params={"question_id": "finory-1", "question": "국내 ETF?"})
        assert response.status_code == 200
        assert response.json()["answer"] == "응답: 국내 ETF?"
        assert client.get("/assets/missing.js").status_code == 404
