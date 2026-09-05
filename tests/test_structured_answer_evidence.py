"""Regression coverage with real planning/SQL compilation and mocked I/O."""
import asyncio
import json
from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.service import PipelineAnswerService, get_answer_service
from app.domain.models import EvidenceBundle, RetrievalSource, ValidationResult
from app.entity.resolver import RegistryEntityResolver
from app.evidence.builder import GenericEvidenceBuilder
from app.evidence.llm_answer import (
    AnswerGenerationError, HyperCLOVAAnswerSettings,
    HyperCLOVAEvidenceAnswerGenerator, _evidence_payload,
)
from app.evidence.quality import StaticFieldQualityProvider
from app.evidence.safe_response import ReasonAwareSafeResponseGenerator
from app.evidence.serializer import serialize_evidence_bundle
from app.evidence.validator import QualityAwareEvidenceValidator
from app.execution.config import ExecutionSettings
from app.execution.executor import QueryExecutor
from app.execution.transforms import InternalTransformExecutor
from app.main import create_app
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry, CanonicalV2QueryCompiler,
    CanonicalV2RDBRetriever, V2SnapshotSelection,
)
from app.retrieval.registry import RetrieverRegistry
from tests.test_m10_9_c1_structured_operations import _ontology, _plan, _planner

QUESTION = "미국에 투자하는 채권 ETF를 알려줘."
NAME = "Alternative Access First Priority CLO Bond ETF"
ENTITY = "etf_gl:AAA"


def _retriever(name=NAME):
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    connection = engine.connect.return_value.__enter__.return_value
    connection.scalar.return_value = 1
    connection.execute.return_value.mappings.return_value.all.return_value = [{
        "entity_id": ENTITY, "entity_kind": "FinancialProduct", "product_type": "ETF",
        "preferred_name": name, "name_status": "AVAILABLE",
    }]
    selector = MagicMock()
    selector.select.return_value = V2SnapshotSelection(
        date(2026, 8, 24), "260824", "merged-optical-1.4", ("snapshot",), ("dataset",)
    )
    retriever = CanonicalV2RDBRetriever(
        engine, CanonicalV2QueryCompiler(CanonicalV2FieldRegistry(), default_limit=100), selector
    )
    retriever._project = MagicMock(return_value={(ENTITY, "product.name"): name})
    retriever._metric_details = MagicMock(return_value={})
    retriever._provenance = MagicMock(return_value={ENTITY: {
        "fact_ids": ["fact-1"], "assertion_ids": ["assertion-1"],
        "source_record_ids": ["record-1"], "dataset_codes": ["PREF02N001"],
    }})
    return retriever, connection


def _evidence(question=QUESTION, name=NAME):
    parsed, grounded, plan = asyncio.run(_plan(question))
    retriever, connection = _retriever(name)
    result = retriever._retrieve_sync(plan.steps[0])
    bundle = asyncio.run(GenericEvidenceBuilder().build(grounded, result.records))
    return grounded, plan, bundle, connection


def _validate(grounded, bundle):
    return asyncio.run(QualityAwareEvidenceValidator(StaticFieldQualityProvider()).validate(grounded, bundle))


def test_executed_constraints_survive_builder_validator_and_serializers():
    grounded, plan, bundle, connection = _evidence()
    assert plan.steps[0].inputs["product_types"] == ["FinancialProduct.ETF"]
    matches = bundle.evidence[0].metadata["structured_constraint_matches"]
    assert {m["canonical_field"]: m["value"] for m in matches} == {
        "product.region": "Region.US", "product.asset_type": "AssetType.Bond",
    }
    assert [m["constraint_id"] for m in matches] == plan.steps[0].inputs["filter_constraint_ids"]
    assert all(m["satisfied"] and m["operator"] == "eq" for m in matches)
    # The real compiler sent both canonical predicates to the execution boundary.
    sql = connection.execute.call_args.args[0].compile()
    assert "ETF" in str(sql.params)
    registry = CanonicalV2FieldRegistry()
    assert registry.concept_iri("EXPOSURE_REGION", "Region.US") in str(sql.params)
    assert registry.concept_iri(registry.field("product.asset_type").semantic_key, "AssetType.Bond") in str(sql.params)
    validation = _validate(grounded, bundle)
    assert validation.answerable
    context = serialize_evidence_bundle(bundle, validation)
    for value in ("fact-1", "assertion-1", "record-1", "PREF02N001", "Region.US", "AssetType.Bond"):
        assert value in context
    payload = json.loads(_evidence_payload(bundle))
    assert payload["contexts"][0]["structured_constraint_matches"] == matches
    assert payload["contexts"][0]["product_type"] == "ETF"
    assert payload["records"][0]["value"] == NAME
    assert "assertion-1" not in _evidence_payload(bundle)


@pytest.mark.parametrize("field", ["product.region", "product.asset_type"])
def test_missing_material_constraint_value_fails_closed(field):
    grounded, _, bundle, _ = _evidence()
    bundle.evidence[0].metadata["structured_constraint_matches"] = [
        m for m in bundle.evidence[0].metadata["structured_constraint_matches"]
        if m["canonical_field"] != field
    ]
    assert not _validate(grounded, bundle).answerable


def test_name_does_not_create_a_region_constraint():
    grounded, _, bundle, _ = _evidence("채권 ETF를 알려줘.", "US Named Bond ETF")
    assert _validate(grounded, bundle).answerable
    payload = json.loads(_evidence_payload(bundle))
    matches = payload["contexts"][0]["structured_constraint_matches"]
    assert all(m["canonical_field"] != "product.region" for m in matches)
    assert "Region.US" not in _evidence_payload(bundle)


def test_losing_both_constraint_names_and_values_still_fails_closed():
    grounded, _, bundle, _ = _evidence(name="US Named Bond ETF")
    bundle.evidence[0].metadata.pop("structured_constraint_matches")
    bundle.evidence[0].metadata.pop("matched_constraints")
    assert not _validate(grounded, bundle).answerable


@pytest.mark.parametrize("key,value", [
    ("canonical_field", []), ("operator", []), ("value", None), ("satisfied", False),
])
def test_invalid_constraint_receipt_is_blocking(key, value):
    grounded, _, bundle, _ = _evidence()
    bundle.evidence[0].metadata["structured_constraint_matches"][0][key] = value
    assert not _validate(grounded, bundle).answerable


@pytest.mark.parametrize("operator,value", [("ne", "Region.US"), ("in", ["Region.US", "Region.IN"])])
def test_non_equality_predicates_are_not_serialized_as_equality(operator, value):
    _, _, plan = asyncio.run(_plan(QUESTION))
    item = next(m for m in plan.steps[0].inputs["filters"] if m["canonical_field"] == "product.region")
    item["raw"]["operator"] = operator
    item["canonical_value"] = value
    retriever, _ = _retriever()
    records = retriever._retrieve_sync(plan.steps[0]).records
    match = next(m for m in records[0].metadata["structured_constraint_matches"] if m["canonical_field"] == "product.region")
    assert match["operator"] == operator and match["value"] == value


def test_oversized_evidence_is_never_silently_cut_or_sent():
    _, _, bundle, _ = _evidence()
    bundle.evidence[0].value = "x" * 25000
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("unexpected HTTP call"))) as client:
            generator = HyperCLOVAEvidenceAnswerGenerator(
                HyperCLOVAAnswerSettings(enabled=True, api_key="test"), http_client=client
            )
            with pytest.raises(AnswerGenerationError, match="context budget"):
                await generator.generate(QUESTION, bundle, ValidationResult(answerable=True))
            with pytest.raises(ValueError, match="validated evidence"):
                await generator.generate(QUESTION, bundle, ValidationResult(answerable=False))
    asyncio.run(run())


def test_server_independent_get_answer_pipeline(monkeypatch):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        prompt = body["messages"][0]["content"]
        assert "다시 판정하지" in prompt and "이름에 US가 있어도" in prompt
        payload = json.loads(json.loads(body["messages"][1]["content"])["validated_evidence"])
        context = payload["contexts"][0]
        assert context["product_type"] == "ETF"
        assert {m["value"] for m in context["structured_constraint_matches"]} == {"Region.US", "AssetType.Bond"}
        # Mock checks the generation input contract, not live model compliance.
        answer = f"미국에 투자하는 채권 ETF: {payload['records'][0]['value']} ({ENTITY})"
        return httpx.Response(200, json={"result": {"message": {"content": answer}}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    generator = HyperCLOVAEvidenceAnswerGenerator(
        HyperCLOVAAnswerSettings(enabled=True, api_key="test"), http_client=client
    )
    retriever, _ = _retriever()
    service = PipelineAnswerService(
        query_analyzer=RuleBasedQueryAnalyzer(), entity_resolver=RegistryEntityResolver(MagicMock()),
        ontology_service=_ontology(), planner=_planner(),
        executor=QueryExecutor(registry=RetrieverRegistry({RetrievalSource.RDB: retriever}),
            transform_executor=InternalTransformExecutor(), settings=ExecutionSettings()),
        evidence_builder=GenericEvidenceBuilder(),
        evidence_validator=QualityAwareEvidenceValidator(StaticFieldQualityProvider()),
        answer_generator=generator, safe_response_generator=ReasonAwareSafeResponseGenerator(),
        close_callbacks=[client.aclose],
    )
    monkeypatch.setattr("app.main.get_answer_service", lambda: service)
    app = create_app()
    app.dependency_overrides[get_answer_service] = lambda: service
    with TestClient(app) as api:
        response = api.get("/answer", params={"question_id": "test-001", "question": QUESTION})
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"question_id", "question", "retrieved_context", "think_trace", "answer"}
    assert all(isinstance(value, str) for value in data.values())
    assert NAME in data["answer"] and "명확하지" not in data["answer"]
    assert "execution_step_id" in data["retrieved_context"]
    assert json.loads(data["think_trace"])["validation_summary"]["answerable"]
    assert len(requests) == 1
