import asyncio
import csv
from pathlib import Path

import pytest
from rdflib import Graph, OWL, RDF, SH, URIRef

from app.graph.backend import Neo4jGraphBackend
from app.graph.config import GraphSettings
from app.graph.extract import CanonicalGraphExtractor
from app.graph.mapping import GraphMappingRegistry
from app.graph.models import GraphBuildData, GraphBuildStats, GraphEdge
from app.ontology.index import FP
from app.ontology.loader import LEGACY_ONTOLOGY_FILES, OntologyLoader, V7_ONTOLOGY_FILES
from app.ontology.models import OntologyLoadError


ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_ROOT = ROOT / "ontology"


def test_v7_loader_uses_canonical_candidate_file() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="v7").load()

    assert tuple(path.relative_to(ONTOLOGY_ROOT).as_posix() for path in loaded.files) == V7_ONTOLOGY_FILES
    assert str(FP.tracksIndex) in loaded.index.object_properties
    assert str(FP.hasShareClass) in loaded.index.object_properties
    assert str(FP.hasSaleLot) in loaded.index.object_properties
    assert str(FP.tracks) not in loaded.index.object_properties
    assert str(FP.identifierType) not in loaded.index.data_properties


def test_legacy_loader_remains_default() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT).load()

    assert tuple(path.relative_to(ONTOLOGY_ROOT).as_posix() for path in loaded.files) == LEGACY_ONTOLOGY_FILES
    GraphMappingRegistry(loaded.index)


def test_v7_graph_mapping_registry_uses_v7_vocabulary() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="v7").load()
    registry = GraphMappingRegistry(loaded.index, version="v7")

    assert registry.runtime_mapping_file == "mappings/v7_runtime_mapping.csv"
    expected = {
        ("managedBy", "ETF", "AssetManagementCompany"),
        ("tracksIndex", "ETF", "Index"),
        ("hasShareClass", "Fund", "FundShareClass"),
        ("hasSaleLot", "Bond", "SaleLot"),
    }
    actual = {
        (item.canonical_relation, item.subject_type, item.object_type)
        for item in registry.mappings
    }
    assert expected <= actual
    assert registry.get("hasSaleLot").source_bindings == ()


def test_domainless_property_still_enforces_declared_range() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="v7").load()

    assert loaded.index.is_compatible("ETF", "managedBy", "AssetManagementCompany")
    assert loaded.index.is_compatible("Bond", "managedBy", "AssetManagementCompany")
    assert not loaded.index.is_compatible("ETF", "managedBy", "Index")
    assert not loaded.index.is_compatible("ETF", "hasShareClass", "FundShareClass")


def test_mixed_legacy_v7_mapping_modes_fail_validation() -> None:
    legacy = OntologyLoader(ONTOLOGY_ROOT).load()
    v7 = OntologyLoader(ONTOLOGY_ROOT, version="v7").load()

    with pytest.raises(OntologyLoadError):
        GraphMappingRegistry(legacy.index, version="v7")
    with pytest.raises(OntologyLoadError):
        GraphMappingRegistry(v7.index)


def test_v7_source_mapping_is_separate_from_legacy_mapping() -> None:
    legacy = ONTOLOGY_ROOT / "mappings" / "column_mapping.csv"
    v7 = ONTOLOGY_ROOT / "mappings" / "v7_runtime_mapping.csv"

    assert legacy.is_file()
    assert v7.is_file()
    assert "MetricObservation" in legacy.read_text(encoding="utf-8-sig")
    v7_text = v7.read_text(encoding="utf-8")
    assert "MetricFamily" in v7_text
    assert "MetricObservation" not in v7_text
    rows = list(csv.DictReader(v7.open(encoding="utf-8")))
    assert any(
        row["v7_resource"] == "hasSaleLot" and row["status"] == "deferred"
        for row in rows
    )


def test_v7_ontology_has_no_undeclared_fin_terms() -> None:
    graph = Graph().parse(ONTOLOGY_ROOT / "candidates" / "new_optical_ontology.ttl", format="turtle")
    used = {
        term
        for triple in graph
        for term in triple
        if isinstance(term, URIRef) and str(term).startswith(str(FP))
    }
    declared = (
        set(graph.subjects(RDF.type, OWL.Class))
        | set(graph.subjects(RDF.type, OWL.ObjectProperty))
        | set(graph.subjects(RDF.type, OWL.DatatypeProperty))
        | set(graph.subjects(RDF.type, OWL.AnnotationProperty))
        | set(graph.subjects(RDF.type, SH.NodeShape))
    )
    for subject, _, _ in graph.triples((None, RDF.type, None)):
        if isinstance(subject, URIRef) and str(subject).startswith(str(FP)):
            declared.add(subject)

    assert used - declared == set()


class RecordingGraphBackend(Neo4jGraphBackend):
    def __init__(self, settings: GraphSettings) -> None:
        super().__init__(driver=None, settings=settings)  # type: ignore[arg-type]
        self.queries: list[str] = []

    async def _execute(self, query, parameters):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return []


def test_backend_write_edges_uses_v7_mapping_version() -> None:
    backend = RecordingGraphBackend(GraphSettings(ontology_version="v7"))
    data = GraphBuildData(
        nodes=(),
        edges=(
            GraphEdge(
                edge_id="edge-1",
                subject_id="etf-1",
                edge_type="TRACKS_INDEX",
                object_id="index-1",
                properties={},
            ),
        ),
        stats=GraphBuildStats(),
    )

    asyncio.run(backend._write_edges(data))

    assert any("TRACKS_INDEX" in query for query in backend.queries)


def test_backend_legacy_default_rejects_v7_edge_type() -> None:
    backend = RecordingGraphBackend(GraphSettings())
    data = GraphBuildData(
        nodes=(),
        edges=(
            GraphEdge(
                edge_id="edge-1",
                subject_id="etf-1",
                edge_type="TRACKS_INDEX",
                object_id="index-1",
                properties={},
            ),
        ),
        stats=GraphBuildStats(),
    )

    with pytest.raises(ValueError, match="unsupported graph edge type"):
        asyncio.run(backend._write_edges(data))


def test_v7_active_runtime_mapping_sources_exist_in_schema() -> None:
    from app.data import schema

    schema_fields = {
        "canonical_products": set(schema.canonical_products.c.keys()),
        "fund_classes": set(schema.fund_classes.c.keys()),
        "funds": set(schema.funds.c.keys()),
        "bond_attributes": set(schema.bond_attributes.c.keys()),
        "etf_attributes": set(schema.etf_attributes.c.keys()),
    }
    rows = list(
        csv.DictReader(
            (ONTOLOGY_ROOT / "mappings" / "v7_runtime_mapping.csv").open(
                encoding="utf-8"
            )
        )
    )
    missing = []
    for row in rows:
        if row["status"] != "active" or row["mapping_kind"] != "ObjectProperty":
            continue
        table, column = row["source_field"].split(".", 1)
        if table not in schema_fields or column not in schema_fields[table]:
            missing.append(row["source_field"])

    assert missing == []


def test_v7_extractor_emits_v7_edges_without_legacy_relation_mix() -> None:
    extractor = CanonicalGraphExtractor(
        engine=None,  # type: ignore[arg-type]
        snapshot="2026-08-21",
        version="v7",
    )

    extractor._extract_product(
        {
            "product_type": "FinancialProduct.ETF",
            "canonical_product_id": "etf-1",
            "product_name": "Example ETF",
            "source_dataset": "domestic_etp",
            "source_record_key": "row-1",
            "asset_manager": "Mirae Asset",
            "base_index": "KOSPI 200",
            "region": "KR",
            "asset_type": "Equity",
            "risk_grade": "2",
            "issuer": None,
            "currency": None,
        }
    )
    extractor._extract_product(
        {
            "product_type": "FinancialProduct.Bond",
            "canonical_product_id": "bond-1",
            "product_name": "Example Bond",
            "source_dataset": "domestic_bond",
            "source_record_key": "row-2",
            "asset_manager": None,
            "base_index": None,
            "region": None,
            "asset_type": None,
            "risk_grade": "1",
            "issuer": "Issuer",
            "currency": "KRW",
        }
    )
    extractor._extract_fund_class(
        {
            "fund_id": "fund-1",
            "canonical_product_id": "class-1",
            "class_code": "C",
            "fund_name": "Example Fund",
            "source_fund_id": "fund-row",
            "issuer": "Manager",
            "base_index": "Benchmark",
            "source_record_key": "class-row",
        }
    )
    extractor._finalize_stats()

    assert set(extractor._stats.edges_by_relation) == {
        "MANAGED_BY",
        "TRACKS_INDEX",
        "HAS_SHARE_CLASS",
    }
    assert all("TRACKS" != edge_type for _, edge_type, _ in extractor._edges)
    assert all("HAS_CLASS" != edge_type for _, edge_type, _ in extractor._edges)
    assert any(
        node.node_type == "AssetManagementCompany"
        for node in extractor._nodes.values()
    )
    assert extractor._nodes["class-1"].node_type == "FundShareClass"


def test_v7_salelot_is_ontology_capability_but_materialization_is_deferred() -> None:
    loaded = OntologyLoader(ONTOLOGY_ROOT, version="v7").load()
    registry = GraphMappingRegistry(loaded.index, version="v7")
    rows = list(
        csv.DictReader(
            (ONTOLOGY_ROOT / "mappings" / "v7_runtime_mapping.csv").open(
                encoding="utf-8"
            )
        )
    )

    assert str(FP.SaleLot) in loaded.index.classes
    assert str(FP.hasSaleLot) in loaded.index.object_properties
    assert loaded.index.is_compatible("Bond", "hasSaleLot", "SaleLot")
    assert registry.get("hasSaleLot").source_bindings == ()
    assert any(
        row["v7_resource"] == "hasSaleLot" and row["status"] == "deferred"
        for row in rows
    )

    extractor = CanonicalGraphExtractor(
        engine=None,  # type: ignore[arg-type]
        snapshot="2026-08-21",
        version="v7",
    )
    extractor._extract_product(
        {
            "product_type": "FinancialProduct.Bond",
            "canonical_product_id": "bond-1",
            "product_name": "Example Bond",
            "source_dataset": "domestic_bond",
            "source_record_key": "row-2",
            "asset_manager": None,
            "base_index": None,
            "region": None,
            "asset_type": None,
            "risk_grade": "1",
            "issuer": "Issuer",
            "currency": "KRW",
        }
    )
    extractor._finalize_stats()

    assert "HAS_SALE_LOT" not in extractor._stats.edges_by_relation
