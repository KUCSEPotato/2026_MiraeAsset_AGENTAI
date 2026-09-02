import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from app.agent.service import create_production_answer_service, get_answer_service
from app.data.database import DatabaseSettings
from app.data.profiling import rebuild_aggregate_profiles
from app.data.schema import canonical_products
from app.main import app
from app.ontology.loader import TEAM_V1_ONTOLOGY_FILES
from app.ontology.loader import OntologyLoader
from app.ontology.models import OntologyLoadError
from app.ontology.rdf_service import RDFOntologyService
from tests.data_helpers import postgres_engine
from tests.test_rdb_retriever import product


pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.skipif(
        not os.getenv("POSTGRES_TEST_DATABASE_URL"),
        reason="disposable PostgreSQL is unavailable",
    ),
]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def real_service(tmp_path: Path):
    engine = postgres_engine(tmp_path / "m8-real")
    bond = product("M8-BOND", aum=100.0, expense_ratio=0.2)
    equity = product(
        "M8-EQUITY",
        asset_type="AssetType.Equity",
        aum=300.0,
        expense_ratio=0.1,
    )
    with engine.begin() as connection:
        connection.execute(insert(canonical_products), [bond, equity])
        rebuild_aggregate_profiles(connection, snapshot="2026-07-11")
    service = create_production_answer_service(
        database_engine=engine,
        database_settings=DatabaseSettings(
            database_url=engine.url.render_as_string(hide_password=False),
            snapshot_date="2026-07-11",
        ),
    )
    return service


def test_production_provider_uses_all_rdf_ontology_files(tmp_path: Path) -> None:
    service = real_service(tmp_path)

    assert isinstance(service._ontology_service, RDFOntologyService)
    assert tuple(
        item.name for item in service._ontology_service.ontology_files
    ) == tuple(Path(item).name for item in TEAM_V1_ONTOLOGY_FILES)


def test_production_provider_does_not_hide_missing_ontology(
    tmp_path: Path,
) -> None:
    missing_loader = OntologyLoader(
        tmp_path / "missing-ontology",
        known_canonical_fields=frozenset(),
    )

    with pytest.raises(OntologyLoadError, match="mandatory ontology files"):
        create_production_answer_service(ontology_loader=missing_loader)


def test_rdf_grounding_continues_through_real_rdb_api(
    client: TestClient,
    tmp_path: Path,
) -> None:
    service = real_service(tmp_path)
    app.dependency_overrides[get_answer_service] = lambda: service

    response = client.get(
        "/answer",
        params={
            "question_id": "M8-E2E",
            "question": "미국에 투자하는 채권 ETF를 알려줘.",
        },
    )
    payload = response.json()
    trace = json.loads(payload["think_trace"])

    assert response.status_code == 200
    assert trace["semantic_summary"]["canonical_concepts"] == [
        "FinancialProduct.ETF",
        "Region.US",
        "AssetType.Bond",
    ]
    assert trace["planner"] == "rule"
    assert "\"real_rdb\":true" in payload["retrieved_context"]
    assert "Product M8-BOND" in payload["answer"]


def test_team_aum_sort_fails_closed_until_unit_contract_is_verified(
    client: TestClient,
    tmp_path: Path,
) -> None:
    service = real_service(tmp_path)
    app.dependency_overrides[get_answer_service] = lambda: service

    response = client.get(
        "/answer",
        params={
            "question_id": "M8-AUM",
            "question": (
                "미국 주식형 ETF 중 순자산이 큰 상품을 알려줘."
            ),
        },
    )
    payload = response.json()
    trace = json.loads(payload["think_trace"])

    assert response.status_code == 200
    assert trace["status"] == "unsupported"
    assert trace["reason"] == "unsupported_constraint"
    assert "Product M8-EQUITY" not in payload["retrieved_context"]
