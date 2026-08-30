import asyncio

import httpx
import pytest

from app.domain.models import Evidence, EvidenceBundle, ValidationResult
from app.evidence.llm_answer import (
    AnswerGenerationError,
    HyperCLOVAAnswerSettings,
    HyperCLOVAEvidenceAnswerGenerator,
)


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


def test_enabled_answer_generation_requires_credentials() -> None:
    with pytest.raises(ValueError, match="CLOVASTUDIO_API_KEY"):
        HyperCLOVAAnswerSettings(enabled=True).validate()
