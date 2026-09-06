from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.domain.models import (
    ConceptCategory,
    QueryOperation,
    QueryStep,
    RetrievalSource,
    SemanticCoverageStatus,
)
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.ontology.runtime_mapping import (
    BOND_TYPE_RESOURCES,
    TeamOntologyRuntimeMapping,
)
from app.planning.coordinator import QueryPlanner
from app.planning.metadata import FieldCapability, RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.llm_parser import hyperclova_candidate_schema
from app.query.semantic_models import LLMSemanticParseCandidate
from app.query.semantic_validation import LLMSemanticCandidateValidator
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    V2SnapshotSelection,
)


def _ontology() -> RDFOntologyService:
    fields = CanonicalV2FieldRegistry().canonical_fields
    return RDFOntologyService(OntologyLoader(
        Path("ontology"), version="team-v1", known_canonical_fields=fields,
    ).load())


def _planner() -> QueryPlanner:
    metadata = RoutingMetadataRegistry()
    return QueryPlanner(
        routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata),
    )


async def _analyze_ground_plan(question: str):
    parsed = await RuleBasedQueryAnalyzer().analyze(question)
    resolved = await RegistryEntityResolver(StaticEntityLookup()).resolve(parsed)
    grounded = await _ontology().ground(resolved)
    return parsed, grounded, await _planner().create_plan(grounded)


def test_collection_projection_is_not_silently_reclassified_as_product_name() -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(
        "ETF의 상품유형과 투자지역을 알려줘"
    ))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.product_types == ["ETF"]
    assert parsed.entities == []
    assert parsed.requested_fields == ["상품유형", "투자지역"]
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.product_type",
        "product.region",
    ]
    assert plan.steps[0].inputs["requested_fields"] == [
        "product.product_type",
        "product.region",
    ]


def test_unknown_collection_projection_remains_fail_closed_not_entity_not_found() -> None:
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("ETF의 비밀지표를 알려줘")
    )

    assert parsed.entities == []
    assert parsed.semantic_coverage is SemanticCoverageStatus.INCOMPLETE
    assert [item.raw_text for item in parsed.unparsed_material_spans] == [
        "비밀지표를"
    ]


def test_collection_relations_do_not_create_a_fake_product_anchor() -> None:
    parsed, _, plan = asyncio.run(_analyze_ground_plan(
        "공모펀드의 운용사와 벤치마크를 알려줘"
    ))

    assert parsed.entities == []
    assert parsed.product_types == ["공모펀드"]
    assert [item.raw_text for item in parsed.relations] == ["운용사", "벤치마크"]
    assert {step.source for step in plan.steps} >= {
        RetrievalSource.RDB,
        RetrievalSource.GRAPH,
    }


@pytest.mark.parametrize("bond_type", sorted(BOND_TYPE_RESOURCES))
def test_every_authoritative_bond_type_alias_reaches_canonical_grounding(
    bond_type: str,
) -> None:
    parsed = asyncio.run(
        RuleBasedQueryAnalyzer().analyze(f"{bond_type} 5개 보여줘")
    )
    grounded = asyncio.run(
        _ontology().ground(
            asyncio.run(RegistryEntityResolver(StaticEntityLookup()).resolve(parsed))
        )
    )

    filters = [item for item in grounded.grounded_filters
               if item.canonical_field == "product.bond_type"]
    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert [item.field for item in parsed.filters] == ["bond_type"]
    assert len(filters) == 1
    assert filters[0].canonical_value is not None
    assert filters[0].canonical_value.value == f"BondType.{bond_type}"


def test_market_scope_and_offering_type_compose_without_exposure_inference() -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(
        "시장범위가 국내인 공모펀드 5개 보여줘"
    ))

    assert parsed.requested_fields == []
    assert [(item.raw_filter.field, item.canonical_field,
             item.canonical_value.value if item.canonical_value else None)
            for item in grounded.grounded_filters] == [
        ("market_scope", "product.market_scope", "MarketScope.Domestic"),
        ("offering_type", "product.offering_type", "OfferingType.PUBLIC"),
    ]
    assert plan.steps[0].source is RetrievalSource.RDB


def test_trading_currency_is_distinct_from_asset_class_currency() -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(
        "USD 거래통화인 ETF 5개 보여줘"
    ))

    assert [(item.field, item.value) for item in parsed.filters] == [
        ("trading_currency", "USD")
    ]
    assert [item.canonical_field for item in grounded.grounded_filters] == [
        "product.trading_currency"
    ]
    assert plan.steps[0].inputs["filters"][0]["canonical_value"] == "USD"


@pytest.mark.parametrize(
    "question",
    [
        "시장범위가 국내인 공모펀드 5개 보여줘",
        "국고채권 5개 보여줘",
        "USD 거래통화인 ETF 5개 보여줘",
    ],
)
def test_new_structured_filters_compile_on_canonical_v2(question: str) -> None:
    _, _, plan = asyncio.run(_analyze_ground_plan(question))
    snapshot = V2SnapshotSelection(
        snapshot_date=date(2026, 8, 24),
        generation="260824",
        ontology_version="merged-optical-1.4",
        snapshot_ids=("organizer",),
        dataset_ids=("PRBD01N001", "PREF01N001", "PRFD01N001"),
    )

    compiled = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100,
    ).compile(plan.steps[0], snapshot)

    assert compiled.statement is not None


def test_nav_is_connected_from_team_grounding_through_v2_sql_compilation() -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(
        "ETF의 NAV를 알려줘"
    ))
    assert parsed.entities == []
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.nav"
    ]

    snapshot = V2SnapshotSelection(
        snapshot_date=date(2026, 8, 24),
        generation="260824",
        ontology_version="merged-optical-1.4",
        snapshot_ids=("organizer",),
        dataset_ids=("PREF01N001",),
    )
    compiled = CanonicalV2QueryCompiler(
        CanonicalV2FieldRegistry(), default_limit=100,
    ).compile(plan.steps[0], snapshot)
    assert compiled.projected_fields == ("product.nav",)


@pytest.mark.parametrize(
    ("field", "capability"),
    [
        ("product.market_scope", FieldCapability.FILTER),
        ("product.bond_type", FieldCapability.FILTER),
        ("product.trading_currency", FieldCapability.FILTER),
        ("product.listing_country", FieldCapability.PROJECT),
        ("product.nav", FieldCapability.PROJECT),
    ],
)
def test_frontend_routing_and_v2_storage_share_active_field_connections(
    field: str,
    capability: FieldCapability,
) -> None:
    assert RoutingMetadataRegistry().supports_field(field, capability)
    assert CanonicalV2FieldRegistry().field(field).canonical_field == field


def test_all_active_rdb_semantics_have_v2_storage_and_matching_routing_ops() -> None:
    metadata = RoutingMetadataRegistry()
    storage = CanonicalV2FieldRegistry()
    operation_capabilities = {
        "filter": FieldCapability.FILTER,
        "ordered_comparison": FieldCapability.FILTER,
        "sort": FieldCapability.SORT,
        "sort_contract": FieldCapability.SORT,
        "project": FieldCapability.PROJECT,
    }

    for mapping in TeamOntologyRuntimeMapping().fields:
        if mapping.capability.value != "active" or "rdb" not in mapping.storage_backend:
            continue
        assert storage.field(mapping.canonical_field).canonical_field == mapping.canonical_field
        expected = frozenset(
            operation_capabilities[operation]
            for operation in mapping.operations
            if operation in operation_capabilities
        )
        assert metadata.field(mapping.canonical_field) is not None
        assert metadata.field(mapping.canonical_field).supported_capabilities == expected


def test_projection_only_metrics_are_not_advertised_as_global_sort_or_filter() -> None:
    metadata = RoutingMetadataRegistry()
    for field in ("product.nav", "product.price", "product.expense_ratio"):
        assert metadata.supports_field(field, FieldCapability.PROJECT)
        assert not metadata.supports_field(field, FieldCapability.FILTER)
        assert not metadata.supports_field(field, FieldCapability.SORT)


def test_new_classification_categories_are_explicit_not_generic_strings() -> None:
    assert ConceptCategory.MARKET_SCOPE.value == "market_scope"
    assert ConceptCategory.BOND_TYPE.value == "bond_type"


def test_v2_collection_projection_fields_are_not_captured_as_entity_name() -> None:
    parsed, grounded, _ = asyncio.run(_analyze_ground_plan(
        "ETF의 상장국가와 거래통화를 알려줘"
    ))

    assert parsed.entities == []
    assert parsed.requested_fields == ["상장국가", "거래통화"]
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        "product.listing_country",
        "product.trading_currency",
    ]


@pytest.mark.parametrize(
    ("question", "target", "entity_type", "target_type"),
    [
        ("삼성전자를 보유한 ETF", "삼성전자", "organization", "Organization"),
        ("005930을 보유한 ETF", "005930", "security", "EquitySecurity"),
    ],
)
def test_llm_relation_types_match_deterministic_relation_contract(
    question: str,
    target: str,
    entity_type: str,
    target_type: str,
) -> None:
    relation_text = question.removesuffix(" ETF")
    product_start = question.rindex("ETF")
    candidate = LLMSemanticParseCandidate.model_validate({
        "intent": "search_product",
        "product_types": [{
            "source_span": {
                "start": product_start,
                "end": product_start + 3,
                "raw_text": "ETF",
            },
            "value": "ETF",
        }],
        "entities": [{
            "source_span": {
                "start": 0,
                "end": len(target),
                "raw_text": target,
            },
            "entity_type": entity_type,
        }],
        "relations": [{
            "source_span": {
                "start": 0,
                "end": len(relation_text),
                "raw_text": relation_text,
            },
            "raw_relation": "보유한",
            "subject_type": "FinancialProduct",
            "target_raw_text": target,
            "target_type": target_type,
        }],
    })
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))
    accepted = LLMSemanticCandidateValidator({
        "fields": [],
        "product_types": ["ETF"],
        "relations": ["보유한"],
    }).validate(
        question,
        parsed,
        candidate,
        model="test",
        rule_latency_ms=0.0,
        llm_latency_ms=0.0,
        prompt_version="test",
        schema_version="test",
    )

    assert accepted.relations[0].target_type == target_type
    schema_target_types = (
        hyperclova_candidate_schema()["properties"]["relations"]["items"]
        ["properties"]["target_type"]["enum"]
    )
    assert target_type in schema_target_types


def test_llm_can_correct_an_unsupported_rule_type_without_losing_its_span() -> None:
    question = "미국은 빼고 다른 지역 ETF를 찾아줘"
    phrase = "미국은 빼고 다른 지역"
    candidate = LLMSemanticParseCandidate.model_validate({
        "intent": "search_product",
        "product_types": [{
            "source_span": {"start": 13, "end": 16, "raw_text": "ETF"},
            "value": "ETF",
        }],
        "filters": [{
            "source_span": {
                "start": 0,
                "end": len(phrase),
                "raw_text": phrase,
            },
            "field": "region",
            "operator": "ne",
            "value": "미국",
        }],
    })
    rule_result = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))

    accepted = LLMSemanticCandidateValidator({
        "fields": ["region"],
        "product_types": ["ETF"],
        "relations": [],
    }).validate(
        question,
        rule_result,
        candidate,
        model="test",
        rule_latency_ms=0.0,
        llm_latency_ms=0.0,
        prompt_version="test",
        schema_version="test",
    )

    assert accepted.filters[0].operator.value == "ne"
    assert accepted.filters[0].value == "미국"
