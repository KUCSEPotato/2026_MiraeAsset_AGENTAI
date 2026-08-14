import json

from app.schemas.agent import AgentResult


class AgentUnavailableError(Exception):
    """Expected dependency failure that can be returned as a degraded answer."""

    def to_result(self) -> AgentResult:
        return AgentResult(
            retrieved_context="",
            think_trace=json.dumps(
                {
                    "steps": ["agent_service"],
                    "status": "degraded",
                    "reason": "agent_unavailable",
                },
                ensure_ascii=False,
            ),
            answer=(
                "현재 제공된 데이터 검색 과정에서 필요한 근거를 "
                "확인하지 못했습니다."
            ),
        )

