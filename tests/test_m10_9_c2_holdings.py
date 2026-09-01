from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pyshacl import validate
from rdflib import Graph, Namespace, OWL, RDF, RDFS

from app.data.holdings import HoldingsIntegrationError, TrustedHoldingsSnapshot
from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    entity_relations,
    holding_fact_details,
    securities,
)
from app.domain.models import ResolvedQuery, ResolutionStatus, RetrievalSource
from app.external_data.holdings.contract import (
    DATA_CUTOFF_DATE,
    PostCutoffHoldingError,
    validate_holdings,
)
from app.external_data.holdings.models import (
    ExternalHolding,
    HoldingValidationStatus,
    IdentityStatus,
    NumericStatus,
    ProductCategory,
    TemporalStatus,
    WeightScale,
    WeightUnit,
)
from app.external_data.models import SourceTrustTier
from app.graph.compiler import GraphQueryCompiler
from app.graph.mapping import GraphMappingRegistry
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.query.analyzer import RuleBasedQueryAnalyzer


FIN = Namespace("https://miraeasset.com/ontology/financial-product#")


def _holding(**updates) -> ExternalHolding:
    values = {
        "holding_record_id": "holding:1",
        "product_category": ProductCategory.DOMESTIC_ETF,
        "product_name_raw": "KODEX 테스트",
        "product_ticker": "999999",
        "product_isin": "KR7999990001",
        "product_source_id": "2ETFTEST",
        "constituent_name_raw": "테스트전자",
        "constituent_ticker": "005930",
        "constituent_isin": None,
        "constituent_source_id": "005930",
        "weight_raw": "24.49",
        "weight_normalized": Decimal("0.2449"),
        "weight_unit": WeightUnit.PERCENT_OF_NON_CASH_ASSETS,
        "weight_scale": WeightScale.PERCENT_POINTS,
        "effective_date": DATA_CUTOFF_DATE,
        "retrieved_at": datetime(2026, 8, 30, tzinfo=UTC),
        "source_record_id": "extrec:1",
        "source_provider": "Samsung Asset Management KODEX",
        "source_url": "https://www.samsungfund.com/api/v1/kodex/product-pdf/2ETFTEST.do",
        "source_trust_tier": SourceTrustTier.AUTHORITATIVE,
        "snapshot_id": "kodex-20260824",
        "identity_status": IdentityStatus.SOURCE_ID_ONLY,
        "product_identity_status": IdentityStatus.VERIFIED_IDENTIFIER,
        "constituent_identity_status": IdentityStatus.SOURCE_ID_ONLY,
        "numeric_status": NumericStatus.PARTIAL,
        "temporal_status": TemporalStatus.EFFECTIVE_DATE_VERIFIED,
        "validation_status": HoldingValidationStatus.VALID,
    }
    values.update(updates)
    return ExternalHolding(**values)


def test_external_contract_is_temporal_and_weight_semantics_are_explicit() -> None:
    row = _holding()
    assert validate_holdings([row, row], snapshot_id=row.snapshot_id) == [row]
    assert row.effective_date == date(2026, 8, 24)
    assert row.retrieved_at.date() == date(2026, 8, 30)
    assert row.weight_normalized == Decimal("0.2449")
    assert row.weight_unit is WeightUnit.PERCENT_OF_NON_CASH_ASSETS
    with pytest.raises(PostCutoffHoldingError):
        validate_holdings(
            [_holding(effective_date=date(2026, 8, 25))],
            snapshot_id="kodex-20260824",
        )


def test_name_only_constituent_remains_explicitly_unproven() -> None:
    row = _holding(
        constituent_ticker=None,
        constituent_source_id=None,
        constituent_identity_status=IdentityStatus.NAME_ONLY,
        validation_status=HoldingValidationStatus.PARTIAL,
    )
    assert row.constituent_identity_status is IdentityStatus.NAME_ONLY
    assert row.constituent_name_raw == "테스트전자"


def test_snapshot_boundary_requires_manifest_and_canonical_snapshot_identity() -> None:
    snapshot = TrustedHoldingsSnapshot(
        external_snapshot_id="kodex-20260824",
        canonical_snapshot_id="canonical:kodex:20260824",
        manifest_schema_version="external-snapshot-manifest-v1",
        manifest_status="READY",
        manifest_sha256="a" * 64,
        manifest_json={"status": "READY"},
        data_cutoff_date=DATA_CUTOFF_DATE,
        artifact_root=Path("."),
        source_records=(),
        holdings=(),
    )
    assert snapshot.external_snapshot_id != snapshot.canonical_snapshot_id
    with pytest.raises(HoldingsIntegrationError, match="not READY"):
        from app.data.holdings import TrustedHoldingsCanonicalIntegrator
        TrustedHoldingsCanonicalIntegrator._validate_snapshot(
            replace(snapshot, manifest_status="PARTIAL")
        )


def test_canonical_schema_has_security_and_temporal_holding_grains() -> None:
    assert CANONICAL_V2_SCHEMA_VERSION == "m10.9-c2.6-canonical-v2"
    assert {fk.referred_table.name for fk in securities.foreign_key_constraints} == {
        "canonical_entities"
    }
    assert holding_fact_details.c.fact_id.foreign_keys
    relation_check = next(
        item for item in entity_relations.constraints
        if str(getattr(item, "name", "")).endswith("relation_type_allowed")
    )
    assert "HOLDS" in str(relation_check.sqltext)
    assert "SECURITY_ISSUED_BY" in str(relation_check.sqltext)
    kind_check = next(
        item for item in canonical_entities.constraints
        if str(getattr(item, "name", "")).endswith("kind_allowed")
    )
    assert "SECURITY" in str(kind_check.sqltext)


def test_team_ontology_v14_has_non_overloaded_security_relations() -> None:
    graph = Graph().parse("ontology/candidates/new_optical_ontology.ttl", format="turtle")
    ontology = next(graph.subjects(RDF.type, OWL.Ontology))
    assert str(graph.value(ontology, OWL.versionInfo)) == "merged-optical-1.4"
    assert (FIN.EquitySecurity, RDFS.subClassOf, FIN.Security) in graph
    assert (FIN.holds, RDFS.domain, FIN.FinancialProduct) in graph
    assert (FIN.holds, RDFS.range, FIN.Security) in graph
    assert (FIN.securityIssuedBy, RDFS.domain, FIN.Security) in graph
    assert (FIN.securityIssuedBy, RDFS.range, FIN.Organization) in graph
    assert (FIN.issuedBy, RDFS.domain, FIN.Security) not in graph


def test_shacl_rejects_holds_targeting_organization() -> None:
    ontology = Graph().parse(
        "ontology/candidates/new_optical_ontology.ttl", format="turtle"
    )
    data = Graph()
    product = FIN.TestETF
    organization = FIN.TestOrganization
    data.add((product, RDF.type, FIN.ETF))
    data.add((organization, RDF.type, FIN.Organization))
    data.add((product, FIN.holds, organization))
    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="none",
    )
    assert not conforms
    assert "HOLDS must connect FinancialProduct to Security" in str(report)


async def _plan(question: str):
    parsed = await RuleBasedQueryAnalyzer().analyze(question)
    ontology = RDFOntologyService(
        OntologyLoader(Path("ontology"), version="team-v1").load()
    )
    resolved = [
        item.model_copy(update={
            "canonical_id": (
                f"organization:test:{item.raw_text}"
                if item.entity_type == "organization"
                else f"security:test:{item.raw_text}"
            ),
            "resolution_status": ResolutionStatus.RESOLVED,
            "confidence": 1.0,
        })
        for item in parsed.entities
    ]
    grounded = await ontology.ground(
        ResolvedQuery(parsed_query=parsed, resolved_entities=resolved)
    )
    return parsed, grounded, await DeterministicSupervisorPlanner().create_plan(grounded)


@pytest.mark.parametrize("name", ["삼성전자", "SK하이닉스", "테스트전자"])
def test_company_holding_queries_require_the_issuer_path(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, grounded, plan = asyncio.run(
        _plan(f"{name}를 보유한 검증된 KODEX ETF 범위를 알려줘")
    )
    assert parsed.product_universe is not None
    assert parsed.product_universe.operands == ["KODEX_LONG_ONLY_COMPATIBLE"]
    assert parsed.relations[0].raw_text == "보유한"
    assert grounded.grounded_relations[0].canonical_relation == "holds"
    graph_step = next(step for step in plan.steps if step.source is RetrievalSource.GRAPH)
    paths = graph_step.inputs["paths"]
    assert [path["relations"] for path in paths] == [
        ["holds", "securityIssuedBy"],
    ]
    assert paths[0]["target_values"] == [None, f"organization:test:{name}"]


def test_graph_compiler_accepts_only_the_reviewed_holdings_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    compiler = GraphQueryCompiler(
        GraphMappingRegistry(version="canonical-v2"),
        snapshot="2026-08-24",
        node_label="M108DNode",
    )
    _, _, plan = asyncio.run(
        _plan("삼성전자를 보유한 검증된 KODEX ETF 범위를 알려줘")
    )
    step = next(item for item in plan.steps if item.source is RetrievalSource.GRAPH)
    compiled = compiler.compile(step, step.inputs["paths"][0], candidate_ids=["etf:1"])
    assert "HOLDS" in compiled.cypher
    assert "SECURITY_ISSUED_BY" in compiled.cypher
    assert "$target_value_1" in compiled.cypher
    assert "삼성전자" not in compiled.cypher
    assert compiled.parameters["target_value_1"] == "organization:test:삼성전자"


def test_direct_ticker_uses_security_path_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(
        _plan("005930을 보유한 검증된 KODEX ETF 범위를 알려줘")
    )
    assert parsed.relations[0].target_type == "EquitySecurity"
    graph_step = next(step for step in plan.steps if step.source is RetrievalSource.GRAPH)
    assert graph_step.inputs["paths"] == [{
        "raw_relations": ["보유한"],
        "constraint_ids": [parsed.relations[0].constraint_id],
        "relations": ["holds"],
        "directions": ["outgoing"],
        "target_values": ["security:test:005930"],
        "target_types": ["EquitySecurity"],
    }]
    compiled = GraphQueryCompiler(
        GraphMappingRegistry(version="canonical-v2"),
        snapshot="2026-08-24",
        node_label="M108DNode",
    ).compile(
        graph_step, graph_step.inputs["paths"][0], candidate_ids=["etf:1"]
    )
    assert "n1.identifier_value = $target_value_0" in compiled.cypher
    assert compiled.parameters["target_value_0"] == "security:test:005930"


def test_exchange_qualified_foreign_ticker_uses_same_generic_security_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(_plan(
        "검증된 iShares ETF 보유종목 범위에서 XNAS:NVDA를 보유한 ETF"
    ))
    assert parsed.product_universe is not None
    assert parsed.product_universe.operands == [
        "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS"
    ]
    assert parsed.relations[0].target_type == "EquitySecurity"
    assert parsed.relations[0].target_value == "XNAS:NVDA"
    assert parsed.relations[0].constraint_id not in plan.unsupported_constraint_ids
    graph_step = next(step for step in plan.steps if step.source is RetrievalSource.GRAPH)
    assert graph_step.inputs["paths"][0]["relations"] == ["holds"]


def test_bare_foreign_ticker_is_a_security_mention_without_global_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(_plan(
        "검증된 iShares ETF 보유종목 범위에서 NVDA를 보유한 ETF"
    ))
    assert parsed.relations[0].target_type == "EquitySecurity"
    assert parsed.relations[0].target_value == "NVDA"
    graph_step = next(step for step in plan.steps if step.source is RetrievalSource.GRAPH)
    assert graph_step.inputs["paths"][0]["relations"] == ["holds"]


def test_generic_and_full_ishares_holdings_scopes_remain_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    generic, _, generic_plan = asyncio.run(_plan("XNAS:NVDA를 보유한 해외 ETF"))
    assert generic.product_universe is not None
    assert generic.product_universe.operands == ["ForeignETF"]
    assert generic.relations[0].constraint_id in generic_plan.unsupported_constraint_ids

    full, _, full_plan = asyncio.run(_plan("XNAS:NVDA를 보유한 iShares ETF"))
    assert full.product_universe is not None
    assert full.product_universe.operands == ["ISHARES_US_FULL"]
    assert full.relations[0].constraint_id in full_plan.unsupported_constraint_ids


def test_plain_kodex_full_company_query_preserves_relation_and_fails_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(
        _plan("KODEX ETF 중 삼성전자를 보유한 상품")
    )
    assert parsed.product_universe is not None
    assert parsed.product_universe.operands == ["KODEX_FULL"]
    assert parsed.relations[0].target_type == "Organization"
    assert parsed.relations[0].constraint_id in plan.unsupported_constraint_ids


def test_ready_kodex_tiger_union_is_generic_and_coverage_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(
        _plan("검증된 KODEX/TIGER 범위에서 삼성전자를 보유한 ETF")
    )
    assert parsed.product_universe is not None
    assert parsed.product_universe.operands == [
        "KODEX_LONG_ONLY_COMPATIBLE", "TIGER_LONG_ONLY_COMPATIBLE",
    ]
    relation_id = parsed.relations[0].constraint_id
    assert relation_id not in plan.unsupported_constraint_ids


def test_ready_domestic_foreign_provider_union_reuses_generic_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    parsed, _, plan = asyncio.run(
        _plan(
            "검증된 KODEX/TIGER/iShares 범위에서 "
            "005930을 보유한 ETF"
        )
    )
    assert parsed.product_universe is not None
    assert parsed.product_universe.operands == [
        "KODEX_LONG_ONLY_COMPATIBLE",
        "TIGER_LONG_ONLY_COMPATIBLE",
        "ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
    ]
    relation_id = parsed.relations[0].constraint_id
    assert relation_id not in plan.unsupported_constraint_ids


def test_plain_tiger_and_domestic_holdings_remain_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    tiger, _, tiger_plan = asyncio.run(_plan("TIGER ETF 중 삼성전자를 보유한 상품"))
    assert tiger.product_universe is not None
    assert tiger.product_universe.operands == ["TIGER_FULL"]
    assert tiger.relations[0].constraint_id in tiger_plan.unsupported_constraint_ids
    domestic, _, domestic_plan = asyncio.run(
        _plan("삼성전자를 보유한 국내 ETF")
    )
    assert domestic.product_universe is not None
    assert domestic.product_universe.operands == ["DomesticETF"]
    assert domestic.relations[0].constraint_id in domestic_plan.unsupported_constraint_ids


def test_holdings_query_is_fail_closed_before_runtime_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", raising=False)
    _, grounded, plan = asyncio.run(_plan("삼성전자를 보유한 ETF"))
    assert grounded.grounded_relations[0].canonical_relation is None
    assert not any(step.source is RetrievalSource.GRAPH for step in plan.steps)
    relation_constraint = next(
        item for item in grounded.semantic_constraints
        if item.semantic_type.value == "relation"
    )
    assert relation_constraint.unsupported_reason is not None


@pytest.mark.parametrize(
    "question",
    ["삼성전자를 보유한 ETF", "삼성전자를 보유한 국내 ETF"],
)
def test_generic_holdings_universes_are_blocked_by_partial_coverage(
    question: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUSTED_HOLDINGS_RUNTIME_ENABLED", "1")
    _, grounded, plan = asyncio.run(_plan(question))
    constraint_id = grounded.grounded_relations[0].constraint_id
    assert constraint_id in plan.unsupported_constraint_ids
    assert not any(step.source is RetrievalSource.GRAPH for step in plan.steps)


def test_no_acceptance_question_or_company_name_is_hard_coded_in_runtime() -> None:
    runtime = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "app/query/analyzer.py",
            "app/planning/supervisor.py",
            "app/graph/compiler.py",
            "app/data/holdings.py",
        )
    )
    assert "삼성전자" not in runtime
    assert "SK하이닉스" not in runtime
    assert "Cambricon" not in runtime
