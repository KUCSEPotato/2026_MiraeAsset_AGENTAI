import asyncio
from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Literal, Namespace, RDF, XSD

from app.data.catalog import DATASET_SPECS
from app.data.mapping import map_product
from app.domain.models import (
    ConceptCategory,
    ConstraintStatus,
    GroundingStatus,
)
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.graph.extract import CanonicalGraphExtractor
from app.graph.mapping import GraphMappingRegistry
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.ontology.runtime_mapping import (
    DATASET_SNAPSHOT,
    ONTOLOGY_URI,
    ONTOLOGY_VERSION,
    SEMANTIC_MAPPING_VERSION,
)
from app.query.analyzer import RuleBasedQueryAnalyzer


ONTOLOGY_ROOT = Path("ontology")
FIN = Namespace(f"{ONTOLOGY_URI}#")
EX = Namespace("https://miraeasset.com/data/m10-7-test/")


def _service() -> RDFOntologyService:
    return RDFOntologyService(
        OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load()
    )


def _ground(question: str):
    async def run():
        parsed = await RuleBasedQueryAnalyzer().analyze(question)
        resolved = await RegistryEntityResolver(
            StaticEntityLookup()
        ).resolve(parsed)
        return await _service().ground(resolved)

    return asyncio.run(run())


def _spec(dataset: str):
    return next(
        item for item in DATASET_SPECS if item.source_dataset == dataset
    )


def test_team_ontology_version_and_mapping_provenance_are_frozen() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load()

    assert loaded.ontology_uri == ONTOLOGY_URI
    assert loaded.ontology_version == ONTOLOGY_VERSION
    assert loaded.semantic_mapping_version == SEMANTIC_MAPPING_VERSION
    assert DATASET_SNAPSHOT == "2026-08-24"


def test_public_fund_is_fund_plus_public_offering_not_a_new_subclass() -> None:
    grounded = _ground("공모펀드를 알려줘.")
    values = [
        item.canonical_concept
        for item in grounded.grounded_concepts
        if item.canonical_concept is not None
    ]

    assert values[0].ontology_uri.endswith("#Fund")
    assert values[0].canonical_name == "FinancialProduct.Fund"
    assert values[0].runtime_key == "FinancialProduct.Fund"
    assert values[1].ontology_uri.endswith("#PUBLIC")
    assert values[1].canonical_name == "OfferingType.PUBLIC"
    assert values[1].runtime_key == "OfferingType.PUBLIC"
    assert [item.canonical_value.value for item in grounded.grounded_filters] == [
        "OfferingType.PUBLIC"
    ]
    assert "FinancialProduct.PublicFund" not in {
        item.value for item in grounded.canonical_concepts
    }
    assert "FinancialProduct.FundShareClass" not in {
        item.value for item in grounded.canonical_concepts
    }


def test_observed_team_controlled_individuals_are_runtime_active() -> None:
    service = _service()

    assert service.resolve_alias(
        "미국", ConceptCategory.EXPOSURE_REGION
    ).status is GroundingStatus.RESOLVED
    assert service.resolve_alias(
        "주식형", ConceptCategory.ASSET_CLASS
    ).status is GroundingStatus.RESOLVED

    grounded = _ground("미국 주식형 ETF 중 순자산이 큰 상품을 알려줘.")
    unsupported = {
        item.raw_text
        for item in grounded.semantic_constraints
        if item.status is ConstraintStatus.UNSUPPORTED
    }
    stripped = {value.strip() for value in unsupported}
    assert "미국" not in stripped
    assert "주식형" not in stripped
    # M10.9-C1 grounds AUM as a semantic metric.  Source/currency scope is
    # validated later by the explicit comparison-contract boundary.
    assert not any("순자산" in value for value in unsupported)
    assert grounded.grounded_sort[0].canonical_field == "product.aum"


def test_risk_grade_relation_target_uses_controlled_runtime_key() -> None:
    grounded = _ground("위험등급이 1등급인 ETF를 알려줘.")

    relation = grounded.grounded_relations[0]
    assert relation.status is GroundingStatus.RESOLVED
    assert relation.target_raw_text == "1등급"
    assert relation.target_value == "RiskGrade.1"


def test_new_fund_parent_adapter_rejects_representative_sentinels() -> None:
    base = {
        "itm_no": "KR5010101611",
        "itm_nm": "테스트 공모펀드",
        "prvo_pbff_desc": "공모",
        "rptt_ksd_itm_no": "KR0000000000",
    }
    unresolved, reason = map_product(
        _spec("public_fund"),
        base,
        source_file="prfd01n001_data.xlsx",
        source_row_number=2,
        snapshot=DATASET_SNAPSHOT,
    )
    parented, parent_reason = map_product(
        _spec("public_fund"),
        {**base, "rptt_ksd_itm_no": "030410046605"},
        source_file="prfd01n001_data.xlsx",
        source_row_number=3,
        snapshot=DATASET_SNAPSHOT,
    )

    assert reason is parent_reason is None
    assert unresolved is not None and unresolved.fund_class is None
    assert parented is not None and parented.fund_class is not None
    assert parented.fund_class["fund_id"] == "fund_family:030410046605"


def test_team_graph_registry_keeps_underlying_and_tracking_distinct() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load()
    registry = GraphMappingRegistry(loaded.index, version="team-v1")

    assert registry.get("hasUnderlyingIndex").edge_type == (
        "HAS_UNDERLYING_INDEX"
    )
    assert registry.get("tracksIndex").edge_type == "TRACKS_INDEX"
    assert registry.get("hasOfferingType").edge_type == "HAS_OFFERING_TYPE"
    assert registry.get("issuedBy").additional_subject_types == ()


def test_team_generic_identifier_resource_does_not_collapse_runtime_fields() -> None:
    service = _service()

    assert service.map_field("티커") == "product.ticker"
    assert service.map_field("ISIN") == "product.isin"


def test_team_graph_extractor_does_not_invent_etn_issuer_or_tracking() -> None:
    extractor = CanonicalGraphExtractor(
        engine=None,  # type: ignore[arg-type]
        snapshot=DATASET_SNAPSHOT,
        version="team-v1",
    )
    extractor._extract_product(
        {
            "product_type": "FinancialProduct.ETN",
            "canonical_product_id": "etn-1",
            "product_name": "Example ETN",
            "source_dataset": "foreign_etf",
            "source_record_key": "row-1",
            "asset_manager": "Source manager field",
            "issuer": "Source manager field",
            "base_index": "S&P 500 TR",
            "region": "Region.US",
            "asset_type": "AssetType.Equity",
            "risk_grade": None,
            "currency": "USD",
        }
    )
    extractor._finalize_stats()

    assert "HAS_UNDERLYING_INDEX" in extractor._stats.edges_by_relation
    assert "ISSUED_BY" not in extractor._stats.edges_by_relation
    assert "TRACKS_INDEX" not in extractor._stats.edges_by_relation


def test_team_manager_semantics_preserve_storage_identity_for_runtime_join() -> None:
    extractor = CanonicalGraphExtractor(
        engine=None,  # type: ignore[arg-type]
        snapshot=DATASET_SNAPSHOT,
        version="team-v1",
    )
    extractor._extract_product(
        {
            "product_type": "FinancialProduct.ETF",
            "canonical_product_id": "etf-1",
            "product_name": "Example ETF",
            "source_dataset": "domestic_etf",
            "source_record_key": "row-1",
            "asset_manager": "삼성",
            "base_index": None,
            "region": None,
            "asset_type": None,
            "risk_grade": None,
            "currency": None,
        }
    )

    manager = next(
        node
        for node in extractor._nodes.values()
        if node.node_type == "AssetManagementCompany"
    )
    assert manager.entity_id.startswith("assetmanager:domestic_etf:")
    assert "AssetManagementCompany" in manager.labels


def test_team_shacl_accepts_representative_materialized_entity_grains() -> None:
    ontology = OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load().graph
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))

    def add_product(resource, product_type, identifier_value: str) -> None:
        identifier = EX[f"identifier-{identifier_value}"]
        source = EX[f"source-{identifier_value}"]
        data.add((resource, RDF.type, product_type))
        data.add((resource, FIN.internalProductID, Literal(identifier_value)))
        data.add((resource, FIN.productName, Literal(f"Product {identifier_value}")))
        data.add((resource, FIN.hasIdentifier, identifier))
        data.add((resource, FIN.hasSourceRecord, source))
        data.add((identifier, RDF.type, FIN.Identifier))
        data.add((identifier, FIN.identifierValue, Literal(identifier_value)))
        data.add((identifier, FIN.identifierScheme, FIN.SOURCE_ID))
        data.add((identifier, FIN.identifierNamespace, Literal("m10.7-test")))
        data.add((identifier, FIN.validationStatus, Literal("VALIDATED")))
        data.add((source, RDF.type, FIN.SourceRecord))
        data.add((source, FIN.sourcePrimaryKey, Literal(identifier_value)))
        data.add((source, FIN.sourceRowNumber, Literal(1, datatype=XSD.positiveInteger)))
        data.add((source, FIN.snapshotDate, Literal(DATASET_SNAPSHOT, datatype=XSD.date)))
        data.add((source, FIN.inDataset, EX.dataset))
        data.add((source, FIN.describesProduct, resource))

    add_product(EX.etf, FIN.ETF, "ETF-1")
    add_product(EX.fund, FIN.Fund, "FUND-1")
    add_product(EX.bond, FIN.Bond, "BOND-1")
    data.add((EX.share_class, RDF.type, FIN.FundShareClass))
    data.add((EX.fund, FIN.hasShareClass, EX.share_class))
    data.add((EX.share_class, FIN.hasOfferingType, FIN.PUBLIC))
    data.add((EX.sale_lot, RDF.type, FIN.SaleLot))
    data.add((EX.bond, FIN.hasSaleLot, EX.sale_lot))

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )

    assert conforms, str(report)


def test_general_provenance_relation_accepts_non_product_evidence_grain() -> None:
    ontology = OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load().graph
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))
    data.add((EX.share_record, RDF.type, FIN.SourceRecord))
    data.add((EX.share_record, FIN.sourcePrimaryKey, Literal("CLASS-1")))
    data.add((EX.share_record, FIN.inDataset, EX.dataset))
    data.add((EX.share_record, FIN.describesEntity, EX.assertion))
    data.add((EX.assertion, RDF.type, FIN.SourceFieldAssertion))

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )

    assert conforms, str(report)
