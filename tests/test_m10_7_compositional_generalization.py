import asyncio
from pathlib import Path

import pytest

from app.domain.models import ConstraintStatus, GroundingStatus
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.ontology.loader import OntologyLoader
from app.ontology.canonical_fields import ONTOLOGY_CANONICAL_FIELDS
from app.ontology.rdf_service import RDFOntologyService
from app.query.analyzer import RuleBasedQueryAnalyzer


def _parse(question: str):
    return asyncio.run(RuleBasedQueryAnalyzer().analyze(question))


def _ground(question: str):
    async def run():
        parsed = await RuleBasedQueryAnalyzer().analyze(question)
        resolved = await RegistryEntityResolver(StaticEntityLookup()).resolve(parsed)
        loaded = OntologyLoader(
            Path("ontology"),
            known_canonical_fields=ONTOLOGY_CANONICAL_FIELDS,
            version="team-v1",
        ).load()
        return await RDFOntologyService(loaded).ground(resolved)

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("question", "product_type", "region", "operator"),
    [
        ("일본을 제외한 ETN의 티커를 알려줘.", "ETN", "일본", "ne"),
        ("인도를 제외한 ETF의 ISIN을 알려줘.", "ETF", "인도", "ne"),
        ("중국을 제외한 펀드의 가격을 알려줘.", "펀드", "중국", "ne"),
    ],
)
def test_held_out_region_negation_composes_with_product_and_projection(
    question: str,
    product_type: str,
    region: str,
    operator: str,
) -> None:
    parsed = _parse(question)

    assert parsed.product_types == [product_type]
    assert [(item.field, item.operator.value, item.value) for item in parsed.filters] == [
        ("region", operator, region)
    ]
    assert parsed.requested_fields
    assert parsed.unparsed_material_spans == []


@pytest.mark.parametrize(
    ("question", "product_type", "filter_field", "operator", "value"),
    [
        ("일본 제외 ETN의 티커를 알려줘.", "ETN", "region", "ne", "일본"),
        (
            "미국에 투자하는 주식형 ETF의 가격을 알려줘.",
            "ETF",
            "region",
            "eq",
            "미국",
        ),
        (
            "채권형이 아닌 ETF의 ISIN을 알려줘.",
            "ETF",
            "asset_type",
            "ne",
            "채권형",
        ),
    ],
)
def test_structured_collection_prefix_is_not_misread_as_named_product(
    question: str,
    product_type: str,
    filter_field: str,
    operator: str,
    value: str,
) -> None:
    parsed = _parse(question)

    assert parsed.entities == []
    assert parsed.product_types == [product_type]
    assert any(
        (item.field, item.operator.value, item.value)
        == (filter_field, operator, value)
        for item in parsed.filters
    )
    assert parsed.requested_fields
    assert parsed.unparsed_material_spans == []


@pytest.mark.parametrize(
    ("question", "relation", "target", "target_type"),
    [
        (
            "NASDAQ 100을 추종하는 ETN의 ISIN을 알려줘.",
            "tracksIndex",
            "NASDAQ 100",
            "Index",
        ),
        (
            "KRW 표시통화 ETF의 가격을 알려줘.",
            "denominatedIn",
            "KRW",
            "Currency",
        ),
        (
            "JPY 표시통화 채권의 ISIN을 알려줘.",
            "denominatedIn",
            "JPY",
            "Currency",
        ),
    ],
)
def test_held_out_relation_targets_use_the_same_grounding_registry(
    question: str,
    relation: str,
    target: str,
    target_type: str,
) -> None:
    grounded = _ground(question)
    parsed_relation = grounded.parsed_query.relations[0]
    grounded_relation = grounded.grounded_relations[0]

    assert parsed_relation.target_value == target
    assert parsed_relation.target_type == target_type
    assert grounded_relation.canonical_relation == relation
    assert grounded_relation.status is GroundingStatus.RESOLVED


def test_held_out_manager_and_requested_field_compose_without_name_branch() -> None:
    grounded = _ground("미래에셋이 운용하는 ETF의 티커를 알려줘.")

    assert grounded.parsed_query.entities[0].raw_text == "미래에셋"
    assert grounded.grounded_relations[0].canonical_relation == "managedBy"
    assert grounded.grounded_requested_fields[0].canonical_field == "product.ticker"


def test_held_out_observed_dimensions_ground_without_special_case() -> None:
    grounded = _ground("인도 주식형 ETF 중 총보수가 낮은 상품을 알려줘.")

    unsupported_raw = {
        item.raw_text.strip()
        for item in grounded.semantic_constraints
        if item.status is ConstraintStatus.UNSUPPORTED
    }
    assert "인도" not in unsupported_raw
    assert "주식형" not in unsupported_raw
    # Ontology grounding preserves the known canonical metric.  Dataset-scale
    # readiness is rejected later by MetricCapabilityRegistry, rather than
    # being misreported as an unknown ontology expression.
    assert grounded.grounded_sort[0].canonical_field == "product.expense_ratio"
    assert "총보수" not in unsupported_raw


@pytest.mark.parametrize("raw", ["채권혼합", "주식혼합"])
def test_observed_mixed_asset_subtypes_use_shared_asset_pipeline(raw: str) -> None:
    grounded = _ground(f"{raw} 펀드를 알려줘.")

    assert grounded.parsed_query.filters[0].value == raw
    assert grounded.grounded_filters[0].canonical_value.runtime_key == (
        "AssetType.Mixed"
    )
    assert grounded.grounded_filters[0].status is GroundingStatus.RESOLVED
    assert grounded.parsed_query.unparsed_material_spans == []


@pytest.mark.parametrize(
    "question",
    [
        "미국 주식형 ETF 중 순자산이 큰 상품을 알려줘.",
        "미국을 제외한 ETF를 알려줘.",
        "채권형이 아닌 ETF를 알려줘.",
        "미국 또는 일본 ETF를 알려줘.",
        "가격과 NAV를 알려줘.",
        "발행사가 대한민국인 채권을 알려줘.",
        "기초지수가 S&P 500인 ETF를 알려줘.",
        "표시통화가 USD인 ETF를 알려줘.",
        "위험등급이 1등급인 ETF를 알려줘.",
        "삼성이 운용하는 ETF의 기초지수를 알려줘.",
    ],
)
def test_team_semantic_audit_has_no_constraint_omission(question: str) -> None:
    grounded = _ground(question)
    parsed_ids = {
        item.constraint_id
        for item in grounded.parsed_query.semantic_constraints
        if item.required
    }
    grounded_by_id = {
        item.constraint_id: item.status
        for item in grounded.semantic_constraints
        if item.required
    }

    assert set(grounded_by_id) == parsed_ids
    assert all(
        status in {ConstraintStatus.GROUNDED, ConstraintStatus.UNSUPPORTED}
        for status in grounded_by_id.values()
    )
