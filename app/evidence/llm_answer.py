"""Bounded HyperCLOVA answer generation from already validated evidence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.domain.models import EvidenceBundle, ValidationResult


class AnswerGenerationError(RuntimeError):
    """Expected upstream answer-generation failure safe for a controlled 5xx."""


@dataclass(frozen=True)
class HyperCLOVAAnswerSettings:
    enabled: bool = False
    api_key: str | None = None
    base_url: str = "https://clovastudio.stream.ntruss.com"
    model: str = "HCX-007"
    timeout_seconds: float = 45.0
    max_completion_tokens: int = 1_024
    max_evidence_characters: int = 24_000

    @classmethod
    def from_env(cls) -> "HyperCLOVAAnswerSettings":
        return cls(
            enabled=os.getenv("HYPERCLOVA_ANSWER_ENABLED", "false").lower()
            in {"1", "true", "yes"},
            api_key=os.getenv("CLOVASTUDIO_API_KEY") or None,
            base_url=os.getenv(
                "HYPERCLOVA_BASE_URL",
                "https://clovastudio.stream.ntruss.com",
            ).rstrip("/"),
            model=os.getenv("HYPERCLOVA_ANSWER_MODEL", os.getenv("HYPERCLOVA_MODEL", "HCX-007")),
            timeout_seconds=float(os.getenv("HYPERCLOVA_ANSWER_TIMEOUT_SECONDS", "45")),
            max_completion_tokens=int(
                os.getenv("HYPERCLOVA_ANSWER_MAX_COMPLETION_TOKENS", "1024")
            ),
            max_evidence_characters=int(
                os.getenv("HYPERCLOVA_ANSWER_MAX_EVIDENCE_CHARACTERS", "24000")
            ),
        )

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key)

    def validate(self) -> None:
        if self.enabled and not self.api_key:
            raise ValueError(
                "CLOVASTUDIO_API_KEY is required when HYPERCLOVA_ANSWER_ENABLED=true"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("HYPERCLOVA_ANSWER_TIMEOUT_SECONDS must be positive")
        if not 1 <= self.max_completion_tokens <= 32_768:
            raise ValueError("invalid HyperCLOVA answer completion-token budget")
        if self.max_evidence_characters < 1_000:
            raise ValueError("HyperCLOVA answer evidence budget is too small")


class HyperCLOVAEvidenceAnswerGenerator:
    """One model call, no agent loop, and no access beyond validated evidence."""

    provider_name = "hyperclova"
    model_calls_per_answer = 1

    def __init__(
        self,
        settings: HyperCLOVAAnswerSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings.validate()
        if not settings.configured:
            raise ValueError("HyperCLOVA answer generation is not configured")
        self._settings = settings
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(
        self,
        question: str,
        evidence: EvidenceBundle,
        validation: ValidationResult,
    ) -> str:
        if not validation.answerable:
            raise ValueError("answer generation requires validated evidence")
        endpoint = (
            f"{self._settings.base_url}/v3/chat-completions/"
            f"{quote(self._settings.model, safe='')}"
        )
        evidence_payload = _evidence_payload(evidence)[
            : self._settings.max_evidence_characters
        ]
        request = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "검증된 금융 데이터 근거만 사용해 한국어로 간결하게 답하세요. "
                        "근거에 없는 사실, 추측, 조언을 추가하지 마세요. 내부 추론 과정은 "
                        "출력하지 말고, 결과가 여러 개이면 상품 식별자와 핵심 값을 구분하세요."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "validated_evidence": evidence_payload},
                        ensure_ascii=False,
                    ),
                },
            ],
            "topP": 0.2,
            "topK": 0,
            "maxCompletionTokens": self._settings.max_completion_tokens,
            "temperature": 0.1,
            "repetitionPenalty": 1.0,
            "stop": [],
        }
        try:
            response = await self._client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self._settings.api_key}",
                    "X-NCP-CLOVASTUDIO-REQUEST-ID": str(uuid4()),
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            content = response.json()["result"]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty answer")
            return content.strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AnswerGenerationError("HyperCLOVA answer generation failed") from exc


def _evidence_payload(evidence: EvidenceBundle) -> str:
    return json.dumps(
        [
            {
                "source_type": item.source_type,
                "source_id": item.source_id,
                "entity_id": item.entity_id,
                "field": item.field,
                "value": item.value,
                "text": item.text,
                "dataset_snapshot": item.dataset_snapshot,
            }
            for item in evidence.evidence
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
