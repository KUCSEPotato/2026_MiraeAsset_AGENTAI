from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app.domain.models import (
    ConceptCategory,
    ConstraintSemanticType,
    EntityMention,
    ParsedQuery,
    QueryOperation,
    QueryIntent,
    QueryStep,
    RetrievalSource,
    SemanticConstraint,
    SemanticCoverageStatus,
    SourceSpan,
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
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.metadata import FieldCapability, RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.exceptions import SemanticCandidateValidationError
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
    assert parsed.entities == []
    assert parsed.product_types == ["채권"]
    assert [item.field for item in parsed.filters] == ["bond_type"]
    assert len(filters) == 1
    assert filters[0].canonical_value is not None
    assert filters[0].canonical_value.value == f"BondType.{bond_type}"


@pytest.mark.parametrize(
    ("question", "limit"),
    [
        ("회사채를 알려줘", None),
        ("회사채 5개를 알려줘", 5),
    ],
)
def test_corporate_bond_shorthand_is_a_classification_not_an_entity(
    question: str,
    limit: int | None,
) -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert parsed.product_types == ["채권"]
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("bond_type", "일반회사채")
    ]
    assert (parsed.result_limit.value if parsed.result_limit else None) == limit
    assert grounded.grounded_filters[0].canonical_value is not None
    assert (
        grounded.grounded_filters[0].canonical_value.value
        == "BondType.일반회사채"
    )
    assert plan.steps[0].inputs["entity_ids"] == []


@pytest.mark.parametrize(
    ("question", "expected_asset_type", "expected_concept"),
    [
        ("채권형을 알려줘", "채권형", "AssetType.Bond"),
        ("주식형을 알려줘", "주식형", "AssetType.Equity"),
        ("혼합형을 알려줘", "혼합자산", "AssetType.Mixed"),
        ("채권형 상품을 알려줘", "채권형", "AssetType.Bond"),
        ("주식형 상품을 알려줘", "주식형", "AssetType.Equity"),
        ("혼합형 상품을 알려줘", "혼합자산", "AssetType.Mixed"),
    ],
)
def test_asset_class_collection_is_not_a_product_entity(
    question: str,
    expected_asset_type: str,
    expected_concept: str,
) -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))
    resolved = asyncio.run(RegistryEntityResolver(StaticEntityLookup()).resolve(parsed))
    grounded = asyncio.run(_ontology().ground(resolved))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("asset_type", expected_asset_type)
    ]
    assert grounded.grounded_filters[0].canonical_value is not None
    assert grounded.grounded_filters[0].canonical_value.value == expected_concept


def test_bond_asset_class_projection_keeps_collection_grain() -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(
        "채권형 상품의 위험등급을 알려줘"
    ))

    assert parsed.entities == []
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("asset_type", "채권형")
    ]
    assert parsed.requested_fields == ["위험등급"]
    assert grounded.grounded_filters[0].canonical_value is not None
    assert grounded.grounded_filters[0].canonical_value.value == "AssetType.Bond"
    assert plan.steps[0].inputs["entity_ids"] == []
    assert plan.steps[0].inputs["filters"][0]["canonical_value"] == "AssetType.Bond"


def test_unsupported_risk_ordering_preserves_supported_bond_asset_universe() -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(
        "위험이 낮은 채권형 상품을 비교해줘"
    ))
    resolved = asyncio.run(RegistryEntityResolver(StaticEntityLookup()).resolve(parsed))
    grounded = asyncio.run(_ontology().ground(resolved))

    assert parsed.entities == []
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("asset_type", "채권형")
    ]
    assert [(item.field, item.direction) for item in parsed.sort] == [
        ("위험", "asc")
    ]
    assert grounded.grounded_filters[0].canonical_value is not None
    assert grounded.grounded_filters[0].canonical_value.value == "AssetType.Bond"
    with pytest.raises(UnsupportedQuerySemanticsError) as exc_info:
        asyncio.run(_planner().create_plan(grounded))
    assert "sort_capability_disabled:product.risk_grade" in exc_info.value.reasons


def test_llm_candidate_may_retype_only_a_known_classification_entity_hint() -> None:
    question = "채권형 상품 여러 개를 비교해줘"
    span = SourceSpan(start=0, end=3)
    legacy_rule_result = ParsedQuery(
        original_question=question,
        intent=QueryIntent.COMPARE_PRODUCTS,
        entities=[EntityMention(
            raw_text="채권형", entity_type="product", constraint_id="C1"
        )],
        semantic_constraints=[SemanticConstraint(
            constraint_id="C1",
            source_span=span,
            raw_text="채권형",
            semantic_type=ConstraintSemanticType.ENTITY,
            payload={"entity_type": "product"},
        )],
    )
    candidate = LLMSemanticParseCandidate.model_validate({
        "intent": "compare_products",
        "filters": [{
            "source_span": {"start": 0, "end": 3, "raw_text": "채권형"},
            "field": "asset_type",
            "operator": "eq",
            "value": "채권형",
        }],
    })
    parsed = LLMSemanticCandidateValidator({
        "fields": ["asset_type"],
        "product_types": [],
        "regions": [],
        "asset_types": ["채권형"],
        "relations": [],
    }).validate(
        question,
        legacy_rule_result,
        candidate,
        model="fixture",
        rule_latency_ms=0,
        llm_latency_ms=0,
        prompt_version="test",
        schema_version="test",
    )

    assert parsed.entities == []
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("asset_type", "채권형")
    ]


def test_llm_candidate_cannot_retype_an_unknown_entity_as_a_filter() -> None:
    question = "미지상품을 알려줘"
    legacy_rule_result = ParsedQuery(
        original_question=question,
        intent=QueryIntent.SEARCH_PRODUCT,
        entities=[EntityMention(
            raw_text="미지상품", entity_type="product", constraint_id="C1"
        )],
        semantic_constraints=[SemanticConstraint(
            constraint_id="C1",
            source_span=SourceSpan(start=0, end=4),
            raw_text="미지상품",
            semantic_type=ConstraintSemanticType.ENTITY,
            payload={"entity_type": "product"},
        )],
    )
    candidate = LLMSemanticParseCandidate.model_validate({
        "intent": "search_product",
        "filters": [{
            "source_span": {"start": 0, "end": 4, "raw_text": "미지상품"},
            "field": "asset_type",
            "operator": "eq",
            "value": "미지상품",
        }],
    })
    validator = LLMSemanticCandidateValidator({
        "fields": ["asset_type"],
        "product_types": [],
        "regions": [],
        "asset_types": ["채권형"],
        "relations": [],
    })

    with pytest.raises(SemanticCandidateValidationError) as exc_info:
        validator.validate(
            question,
            legacy_rule_result,
            candidate,
            model="fixture",
            rule_latency_ms=0,
            llm_latency_ms=0,
            prompt_version="test",
            schema_version="test",
        )
    assert "candidate_omits_rule_material" in exc_info.value.reasons


@pytest.mark.parametrize(
    "question",
    [
        "달러로 거래되는 ETF를 알려줘",
        "미국 달러로 거래되는 ETF를 알려줘",
        "달러화로 거래되는 ETF를 알려줘",
        "미 달러로 거래되는 ETF를 알려줘",
        "USD로 거래되는 ETF를 알려줘",
        "거래통화가 USD인 ETF를 알려줘",
        "거래통화가 달러인 ETF를 알려줘",
    ],
)
def test_usd_trading_currency_lexical_aliases_share_one_constraint(
    question: str,
) -> None:
    parsed, grounded, plan = asyncio.run(_analyze_ground_plan(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert parsed.product_types == ["ETF"]
    assert [(item.field, item.value) for item in parsed.filters] == [
        ("trading_currency", "USD")
    ]
    assert [item.canonical_field for item in grounded.grounded_filters] == [
        "product.trading_currency"
    ]
    assert plan.steps[0].inputs["filters"][0]["canonical_value"] == "USD"


@pytest.mark.parametrize(
    ("question", "field", "value"),
    [
        ("대체투자 ETF를 알려줘", "asset_type", "대체투자"),
        ("Real Estate ETF를 알려줘", "asset_type", "Real Estate"),
        ("Japan ETF를 알려줘", "region", "Japan"),
        ("사모 펀드를 알려줘", "offering_type", "사모"),
        ("Public 펀드를 알려줘", "offering_type", "Public"),
        ("Private 펀드를 알려줘", "offering_type", "Private"),
        (
            "판매중 펀드를 알려줘",
            "subscription_status",
            "SubscriptionStatus.OPEN_FOR_SUBSCRIPTION",
        ),
        (
            "판매완료 펀드를 알려줘",
            "subscription_status",
            "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
        ),
        (
            "가입 종료 펀드를 알려줘",
            "subscription_status",
            "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
        ),
        (
            "추가매수 종료 펀드를 알려줘",
            "subscription_status",
            "SubscriptionStatus.CLOSED_FOR_SUBSCRIPTION",
        ),
    ],
)
def test_existing_controlled_vocabulary_is_not_reclassified_as_product_name(
    question: str,
    field: str,
    value: object,
) -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert [(item.field, item.value) for item in parsed.filters] == [
        (field, value)
    ]


@pytest.mark.parametrize(
    ("question", "field"),
    [
        ("현재 구매 가능한 ETF를 알려줘", "current_etp_sale_eligible"),
        ("현재 판매 중인 ETF를 알려줘", "current_etp_sale_eligible"),
        ("거래정지가 아닌 ETF를 알려줘", "etp_trading_status"),
        ("정보가 부족한 ETF를 알려줘", "etp_insufficient_info"),
        ("현재 판매 가능한 원화채권을 알려줘", "current_sale_available"),
        ("구매 가능한 채권을 알려줘", "current_bond_purchase_eligible"),
        ("장내 채권을 알려줘", "bond_market_presence"),
        ("판매 LOT이 있는 채권을 알려줘", "has_sale_lot"),
        ("최신 기준가가 있는 펀드를 알려줘", "latest_fund_price_available"),
        (
            "현재 미래에셋에서 가입할 수 있는 공모펀드를 알려줘",
            "current_fund_subscription_eligible",
        ),
        ("지금 추가매수 가능한 펀드를 알려줘", "current_fund_subscription_eligible"),
        (
            "미래에셋에서 판매 중인 공모펀드를 알려줘",
            "current_fund_subscription_eligible",
        ),
    ],
)
def test_existing_structured_filter_span_is_not_reclassified_as_product_name(
    question: str,
    field: str,
) -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert field in {item.field for item in parsed.filters}


@pytest.mark.parametrize(
    ("question", "raw_field", "canonical_field"),
    [
        ("ETF의 상품명을 알려줘", "상품명", "product.name"),
        ("ETF의 단축명을 알려줘", "단축명", "product.short_name"),
        ("ETF의 표준코드를 알려줘", "표준코드", "product.isin"),
        ("ETF의 지역을 알려줘", "지역", "product.region"),
        ("ETF의 국내외구분을 알려줘", "국내외구분", "product.market_scope"),
        ("ETF의 채권유형을 알려줘", "채권유형", "product.bond_type"),
        ("ETF의 종가를 알려줘", "종가", "product.price"),
        ("ETF의 통화를 알려줘", "통화", "product.currency"),
        ("펀드의 가입 상태를 알려줘", "가입 상태", "product.subscription_status"),
        ("ETF의 상품판매여부를 알려줘", "상품판매여부", "product.etp_distribution_status"),
        ("ETF의 거래정지 상태를 알려줘", "거래정지 상태", "product.etp_trading_status"),
    ],
)
def test_active_field_alias_is_not_shadowed_by_shorter_classification_alias(
    question: str,
    raw_field: str,
    canonical_field: str,
) -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))
    resolved = asyncio.run(RegistryEntityResolver(StaticEntityLookup()).resolve(parsed))
    grounded = asyncio.run(_ontology().ground(resolved))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert parsed.filters == []
    assert parsed.requested_fields == [raw_field]
    assert [item.canonical_field for item in grounded.grounded_requested_fields] == [
        canonical_field
    ]


def test_currency_classification_and_currency_projection_keep_their_context() -> None:
    classification = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("통화 ETF를 알려줘")
    )
    projection = asyncio.run(
        RuleBasedQueryAnalyzer().analyze("ETF의 통화를 알려줘")
    )

    assert [(item.field, item.value) for item in classification.filters] == [
        ("asset_type", "통화")
    ]
    assert classification.requested_fields == []
    assert projection.filters == []
    assert projection.requested_fields == ["통화"]


@pytest.mark.parametrize(
    ("question", "product_types", "filters"),
    [
        ("Bond를 알려줘", ["Bond"], []),
        ("Fund를 알려줘", ["Fund"], []),
        ("Bond ETF를 알려줘", ["ETF"], [("asset_type", "Bond")]),
    ],
)
def test_existing_english_product_aliases_are_structured_not_entities(
    question: str,
    product_types: list[str],
    filters: list[tuple[str, object]],
) -> None:
    parsed = asyncio.run(RuleBasedQueryAnalyzer().analyze(question))

    assert parsed.semantic_coverage is SemanticCoverageStatus.COMPLETE
    assert parsed.entities == []
    assert parsed.product_types == product_types
    assert [(item.field, item.value) for item in parsed.filters] == filters


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
        "회사채 5개를 알려줘",
        "달러로 거래되는 ETF를 알려줘",
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
