import json
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert

from app.agent.service import (
    PipelineAnswerService,
    create_production_answer_service,
    get_answer_service,
)
from app.data.database import DatabaseSettings
from app.data.profiling import rebuild_aggregate_profiles
from app.data.schema import canonical_products
from app.main import create_app
from tests.data_helpers import postgres_engine
from tests.test_rdb_retriever import product


def real_service(tmp_path: Path) -> tuple[PipelineAnswerService, Engine]:
    engine = postgres_engine(tmp_path / "real-service")
    rows = [
        product("etf-gl:US-BOND-1", aum=300, expense_ratio=0.1),
        product("etf-gl:US-BOND-2", aum=100, expense_ratio=None),
    ]
    for index, row in enumerate(rows, start=1):
        row.update(
            {
                "source_dataset": "foreign_etf",
                "source_record_key": f"US-BOND-{index}",
                "product_name": f"Actual Test Bond ETF {index}",
                "normalized_product_name": f"actualtestbondetf{index}",
            }
        )
    with engine.begin() as connection:
        connection.execute(insert(canonical_products), rows)
        rebuild_aggregate_profiles(connection, snapshot="2026-07-11")
    settings = DatabaseSettings(
        database_url=engine.url.render_as_string(hide_password=False),
        snapshot_date="2026-07-11",
        rdb_default_limit=10,
    )
    service = create_production_answer_service(
        database_engine=engine, database_settings=settings
    )
    return service, engine


def _build_test_app(service: PipelineAnswerService) -> FastAPI:
    """Wire one test-owned service into both DI and application lifespan."""
    application = create_app()

    @asynccontextmanager
    async def owned_lifespan(app: FastAPI):
        await service.validate_derived_stores()
        app.state.runtime_health = service.runtime_health()
        try:
            yield
        finally:
            await service.close()

    application.router.lifespan_context = owned_lifespan
    application.dependency_overrides[get_answer_service] = lambda: service
    return application


@contextmanager
def owned_client(tmp_path: Path) -> Iterator[TestClient]:
    service, engine = real_service(tmp_path)
    application = _build_test_app(service)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.dependency_overrides.clear()
        engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with owned_client(tmp_path) as test_client:
        yield test_client


def test_real_rdb_api_returns_actual_provenance(
    client: TestClient,
) -> None:
    response = client.get(
        "/answer",
        params={
            "question_id": "M7-REAL",
            "question": "미국에 투자하는 채권 ETF를 알려줘.",
        },
    )

    payload = response.json()
    trace = json.loads(payload["think_trace"])
    assert response.status_code == 200
    assert trace["status"] == "success"
    assert trace["planner"] == "rule"
    assert "source_dataset\":\"foreign_etf" in payload["retrieved_context"]
    assert "real_rdb\":true" in payload["retrieved_context"]
    assert "Actual Test Bond ETF 1" in payload["answer"]
    assert "fake" not in payload["retrieved_context"].casefold()


def test_real_quality_profile_blocks_partial_coverage_ranking(
    client: TestClient,
) -> None:
    response = client.get(
        "/answer",
        params={
            "question_id": "M7-COVERAGE",
            "question": "총보수가 가장 낮은 ETF를 찾아줘.",
        },
    )

    payload = response.json()
    trace = json.loads(payload["think_trace"])
    assert response.status_code == 200
    assert trace["status"] == "unsupported"
    assert trace["reason"] == "unsupported_constraint"
    assert trace["validation_summary"]["reason_codes"] == [
        "UNSUPPORTED_CONSTRAINT"
    ]
    assert "조건을 조금 더 구체적으로 지정" in payload["answer"]
    assert "real_rdb\":true" not in payload["retrieved_context"]


def test_repeated_testclient_lifecycle_shuts_down_cleanly(
    tmp_path: Path,
) -> None:
    for cycle in range(2):
        with owned_client(tmp_path / f"cycle-{cycle}") as test_client:
            health = test_client.get("/health")
            response = test_client.get(
                "/answer",
                params={
                    "question_id": f"M7-LIFECYCLE-{cycle}",
                    "question": "미국에 투자하는 채권 ETF를 알려줘.",
                },
            )

            assert health.status_code == 200
            assert response.status_code == 200
            assert "Actual Test Bond ETF 1" in response.json()["answer"]
