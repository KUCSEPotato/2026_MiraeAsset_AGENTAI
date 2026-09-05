"""Entity/field alignment and truthful Boolean evidence at the answer boundary."""
import asyncio
import json

import pytest

from app.data.metric_capabilities import PREF01_AUM, PREF01_SIX_MONTH_RETURN
from app.domain.models import (
    BooleanExpression, ComparisonSpec, EntityMention, Evidence, EvidenceBundle,
    GroundedField, GroundedQuery, ParsedQuery, ProductUniverseUnion, ValidationResult,
)
from app.evidence.answer import DeterministicEvidenceAnswerGenerator
from app.evidence.llm_answer import _evidence_payload
from app.evidence.quality import StaticFieldQualityProvider
from app.evidence.validator import QualityAwareEvidenceValidator
from app.evidence.builder import GenericEvidenceBuilder
from tests.test_structured_answer_evidence import _retriever
from tests.test_m10_9_c1_structured_operations import _plan, _planner


FIELDS = ["product.aum", "product.six_month_return"]
IDS = ["etf_kr:fixture_alpha", "etf_kr:fixture_beta"]


def _query():
    parsed = ParsedQuery(
        original_question="fixture comparison", intent="compare_products",
        product_universe=ProductUniverseUnion(operands=["DomesticETF"]),
        requested_fields=FIELDS, comparison=ComparisonSpec(fields=FIELDS),
    )
    return GroundedQuery(parsed_query=parsed,
        resolved_entities=[EntityMention(raw_text=value, entity_type="product", canonical_id=value,
            resolution_status="resolved") for value in IDS],
        canonical_fields={field: field for field in FIELDS},
        grounded_requested_fields=[GroundedField(raw_text=field, canonical_field=field, status="resolved") for field in FIELDS])


def _bundle():
    records = []
    contracts = [PREF01_AUM, PREF01_SIX_MONTH_RETURN]
    for index, entity_id in enumerate(IDS):
        for field, contract in zip(FIELDS, contracts, strict=True):
            records.append(Evidence(
                source_type="rdb", source_id=f"fact:{entity_id}:{field}", entity_id=entity_id,
                field=field, value=str(10 + index), dataset_snapshot="2026-08-24",
                observed_at="2026-08-24", metadata={
                    "comparison_contracts": [item.as_plan_input() for item in contracts],
                    "metric_dataset": contract.dataset, "metric_unit": contract.unit,
                    "metric_scale_basis": contract.scale, "metric_currency": contract.currency,
                    "field_fact_id": f"fact:{entity_id}:{field}",
                    "field_evidence_assertion_ids": [f"assertion:{entity_id}:{field}"],
                },
            ))
    return EvidenceBundle(question="fixture comparison", evidence=records)


def _validate(query, bundle):
    return asyncio.run(QualityAwareEvidenceValidator(StaticFieldQualityProvider()).validate(query, bundle))


def test_complete_two_entity_two_field_comparison_is_answerable():
    assert _validate(_query(), _bundle()).answerable


def test_each_entity_requires_each_field_even_if_field_exists_elsewhere():
    bundle = _bundle()
    bundle.evidence.pop()
    result = _validate(_query(), bundle)
    assert result.answerability == "PARTIALLY_ANSWERABLE"
    assert not result.comparison_completed
    assert FIELDS[1] in result.missing_fields
    assert any(finding.entity_id == IDS[1] and finding.field == FIELDS[1] for finding in result.findings)


def test_missing_comparison_entity_cannot_be_omitted():
    bundle = _bundle()
    bundle.evidence = bundle.evidence[:2]
    result = _validate(_query(), bundle)
    assert result.answerability == "PARTIALLY_ANSWERABLE"
    assert not result.comparison_completed
    assert any(finding.entity_id == IDS[1] for finding in result.findings)


@pytest.mark.parametrize("key,value", [
    ("metric_dataset", "PREF02N001"), ("metric_unit", "RATIO"),
    ("metric_scale_basis", "UNKNOWN"), ("metric_currency", "USD"),
    ("field_fact_id", None), ("field_evidence_assertion_ids", []),
])
def test_comparison_evidence_must_match_contract_and_provenance(key, value):
    bundle = _bundle()
    bundle.evidence[0].metadata[key] = value
    result = _validate(_query(), bundle)
    assert result.answerability == "PARTIALLY_ANSWERABLE"
    assert not result.comparison_completed
    invalid = next(item for item in result.clauses if item.kind == "OUTPUT" and item.entity_id == IDS[0] and item.field == FIELDS[0])
    assert invalid.status == "UNSUPPORTED" and invalid.evidence_indices == []


def test_cross_store_snapshots_are_checked_even_for_different_fields():
    bundle = _bundle()
    bundle.evidence.append(Evidence(source_type="graph", source_id="edge:1", entity_id=IDS[0],
        field="graph.holds", value="security:fixture", dataset_snapshot="2026-08-23"))
    result = _validate(_query(), bundle)
    assert not result.answerable
    assert "SNAPSHOT_MISMATCH" in result.reason_codes


def test_or_receipts_preserve_expression_without_claiming_all_branches_true():
    _, query, _ = asyncio.run(_plan("미국에 투자하는 채권 ETF를 알려줘."))
    query.parsed_query.boolean_expression = BooleanExpression(node_type="or", children=[
        BooleanExpression(node_type="predicate", constraint_id=item.raw_filter.constraint_id)
        for item in query.grounded_filters
    ])
    plan = asyncio.run(_planner().create_plan(query))
    retriever, connection = _retriever()
    records = retriever._retrieve_sync(plan.steps[0]).records
    bundle = asyncio.run(GenericEvidenceBuilder().build(query, records))
    assert _validate(query, bundle).answerable
    receipts = records[0].metadata["structured_constraint_matches"]
    assert all(item["satisfied"] is None for item in receipts)
    assert " OR " in str(connection.execute.call_args.args[0].compile())
    payload = json.loads(_evidence_payload(bundle))
    assert payload["contexts"][0]["structured_boolean_expression"]["node_type"] == "or"
    bundle.evidence[0].metadata.pop("structured_boolean_expression")
    assert not _validate(query, bundle).answerable


def test_deterministic_projection_render_does_not_drop_fields_after_ten_records():
    bundle = _bundle()
    bundle.evidence = [bundle.evidence[0].model_copy(update={"entity_id": f"fixture:{number}", "value": f"value{number}"})
                       for number in range(12)]
    answer = asyncio.run(DeterministicEvidenceAnswerGenerator().generate(
        "fixture", bundle, ValidationResult(answerable=True)))
    assert "value11" in answer and "fixture:11" in answer
