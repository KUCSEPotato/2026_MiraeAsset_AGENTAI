import json
from typing import Protocol

from app.schemas.agent import AgentResult


class AnswerService(Protocol):
    async def answer(self, question: str) -> AgentResult:
        """Return an evidence-grounded answer result for a question."""
        ...


class DeterministicBaselineService:
    """Safe placeholder used until retrieval and generation are connected."""

    async def answer(self, question: str) -> AgentResult:
        return AgentResult(
            retrieved_context="",
            think_trace=json.dumps(
                {
                    "steps": ["request_validation", "baseline_agent"],
                    "status": "unanswerable",
                    "reason": "retrieval_not_connected",
                },
                ensure_ascii=False,
            ),
            answer=(
                "현재 평가 API baseline에는 실제 검색 Agent가 연결되어 "
                "있지 않아 근거 기반 답변을 생성할 수 없습니다."
            ),
        )


_baseline_service = DeterministicBaselineService()


def get_answer_service() -> AnswerService:
    return _baseline_service

