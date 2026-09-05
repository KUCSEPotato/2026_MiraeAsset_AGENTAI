"""Verify the UI and API are available on the same application origin."""
from fastapi.testclient import TestClient

from app.agent.service import get_answer_service
from app.main import create_app
from app.schemas.agent import AgentResult


class AnswerStub:
    async def answer(self, question: str) -> AgentResult:
        return AgentResult(answer=f"응답: {question}", retrieved_context="{}", think_trace="{}")


def test_frontend_and_answer_share_origin():
    application = create_app()
    application.dependency_overrides[get_answer_service] = AnswerStub
    with TestClient(application) as client:
        for path in ("/", "/chat", "/chat/"):
            response = client.get(path)
            assert response.status_code == 200
            assert '/assets/app.js' in response.text
        for name in ("app.js", "styles.css", "logo.png", "ory.png"):
            assert client.get(f"/assets/{name}").status_code == 200
        response = client.get("/answer", params={"question_id": "finory-1", "question": "국내 ETF?"})
        assert response.status_code == 200
        assert response.json()["answer"] == "응답: 국내 ETF?"
        assert client.get("/assets/missing.js").status_code == 404
