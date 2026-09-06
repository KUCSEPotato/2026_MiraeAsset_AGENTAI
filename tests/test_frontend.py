"""Verify backend API behavior with and without local static UI assets."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.service import get_answer_service
import app.main as main_module
from app.main import create_app
from app.schemas.agent import AgentResult


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
MISSING_FRONTEND = ROOT / "tests" / "fixtures" / "missing-frontend"


class AnswerStub:
    async def answer(self, question: str) -> AgentResult:
        return AgentResult(answer=f"answer: {question}", retrieved_context="{}", think_trace="{}")


def _override_frontend_paths(
    monkeypatch,
    *,
    frontend_dir: Path,
    export_dir: Path,
    index: Path,
    legacy_index: Path,
) -> None:
    monkeypatch.setattr(main_module, "FRONTEND_DIR", frontend_dir)
    monkeypatch.setattr(main_module, "FRONTEND_EXPORT_DIR", export_dir)
    monkeypatch.setattr(main_module, "FRONTEND_INDEX", index)
    monkeypatch.setattr(main_module, "LEGACY_FRONTEND_INDEX", legacy_index)


def test_backend_serves_api_without_frontend(monkeypatch):
    _override_frontend_paths(
        monkeypatch,
        frontend_dir=MISSING_FRONTEND,
        export_dir=MISSING_FRONTEND / "out",
        index=MISSING_FRONTEND / "out" / "index.html",
        legacy_index=MISSING_FRONTEND / "index.html",
    )

    application = create_app()
    application.dependency_overrides[get_answer_service] = AnswerStub
    with TestClient(application) as client:
        for path in ("/", "/chat", "/assets/app.js"):
            assert client.get(path).status_code == 404
        response = client.get(
            "/answer",
            params={"question_id": "finory-1", "question": "ETF?"},
        )
        assert response.status_code == 200
        assert response.json()["answer"] == "answer: ETF?"
        assert client.get("/assets/missing.js").status_code == 404


def test_backend_serves_legacy_frontend_when_static_files_exist(monkeypatch):
    _override_frontend_paths(
        monkeypatch,
        frontend_dir=FRONTEND,
        export_dir=MISSING_FRONTEND / "out",
        index=MISSING_FRONTEND / "out" / "index.html",
        legacy_index=FRONTEND / "index.html",
    )

    application = create_app()
    application.dependency_overrides[get_answer_service] = AnswerStub
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        assert client.get("/chat").status_code == 200
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404


def test_backend_serves_exported_frontend_when_next_export_exists(monkeypatch):
    _override_frontend_paths(
        monkeypatch,
        frontend_dir=FRONTEND,
        export_dir=FRONTEND / "out",
        index=FRONTEND / "out" / "index.html",
        legacy_index=FRONTEND / "index.html",
    )

    application = create_app()
    application.dependency_overrides[get_answer_service] = AnswerStub
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        assert client.get("/chat").status_code == 200
        assert client.get("/assets/logo.png").status_code == 200
        assert client.get("/_next/static/chunks/139.7a5a8e93a21948c1.js").status_code == 200
        assert client.get("/assets/app.js").status_code == 404
