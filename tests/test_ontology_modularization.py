from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, XSD
from rdflib.compare import isomorphic

from app.ontology.loader import OntologyLoader, TEAM_V1_ONTOLOGY_FILES
from app.ontology.models import OntologyLoadError


ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_ROOT = ROOT / "ontology"
BASELINE = ONTOLOGY_ROOT / "candidates" / "new_optical_ontology.ttl"
FIN = Namespace("https://miraeasset.com/ontology/financial-product#")
EX = Namespace("https://miraeasset.com/data/modularization-test/")


def _baseline() -> Graph:
    return Graph().parse(BASELINE, format="turtle")


def _modules() -> Graph:
    return OntologyLoader(ONTOLOGY_ROOT, version="team-v1").load().graph


def _semantic_set(graph: Graph, predicate) -> set[tuple]:
    return set(graph.triples((None, predicate, None)))


def _identifier(data: Graph, resource, value: str) -> None:
    data.add((resource, RDF.type, FIN.Identifier))
    data.add((resource, FIN.identifierScheme, FIN.ISIN))
    data.add((resource, FIN.identifierValue, Literal(value)))
    data.add((resource, FIN.identifierNamespace, Literal("iso-6166")))
    data.add((resource, FIN.validationStatus, Literal("VALIDATED")))


def _representative_data(*, invalid_holding: bool = False) -> Graph:
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))
    data.add((EX.product, RDF.type, FIN.ETF))
    data.add((EX.product, FIN.internalProductID, Literal("ETF-1")))
    data.add((EX.product, FIN.productName, Literal("Example ETF")))
    data.add((EX.product, FIN.hasIdentifier, EX.product_identifier))
    data.add((EX.product, FIN.hasSourceRecord, EX.source))
    _identifier(data, EX.product_identifier, "KR7000000000")

    data.add((EX.security, RDF.type, FIN.Security))
    data.add((EX.security, RDF.type, FIN.EquitySecurity))
    data.add((EX.security, FIN.hasIdentifier, EX.security_identifier))
    data.add((EX.security, FIN.securityIssuedBy, EX.issuer))
    _identifier(data, EX.security_identifier, "KR7000000018")
    data.add((EX.issuer, RDF.type, FIN.Organization))

    data.add((EX.product, FIN.holds, EX.security))
    if invalid_holding:
        data.add((EX.product, FIN.holds, EX.issuer))

    data.add((EX.source, RDF.type, FIN.SourceRecord))
    data.add((EX.source, FIN.sourcePrimaryKey, Literal("row-1")))
    data.add(
        (
            EX.source,
            FIN.sourceRowNumber,
            Literal(1, datatype=XSD.positiveInteger),
        )
    )
    data.add((EX.source, FIN.inDataset, EX.dataset))
    data.add((EX.source, FIN.describesEntity, EX.product))
    return data


def _conforms(ontology: Graph, data: Graph) -> tuple[bool, str]:
    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )
    return bool(conforms), str(report)


def test_required_submission_modules_exist_parse_and_are_nonempty() -> None:
    assert TEAM_V1_ONTOLOGY_FILES == (
        "common.ttl",
        "bond_kr.ttl",
        "etf_kr.ttl",
        "etf_gl.ttl",
        "fund_pub.ttl",
    )
    for relative in TEAM_V1_ONTOLOGY_FILES:
        path = ONTOLOGY_ROOT / relative
        assert path.is_file()
        assert len(Graph().parse(path, format="turtle")) > 0


def test_module_union_is_graph_isomorphic_to_merged_baseline() -> None:
    before = _baseline()
    after = _modules()

    assert len(before) == len(after) == 1_276
    assert isomorphic(before, after)


@pytest.mark.parametrize(
    "rdf_type",
    (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty),
)
def test_declared_uri_sets_are_identical(rdf_type) -> None:
    before = _baseline()
    after = _modules()
    assert set(before.subjects(RDF.type, rdf_type)) == set(
        after.subjects(RDF.type, rdf_type)
    )


@pytest.mark.parametrize(
    "predicate",
    (
        RDFS.subClassOf,
        RDFS.subPropertyOf,
        RDFS.domain,
        RDFS.range,
        OWL.disjointWith,
        OWL.inverseOf,
        OWL.equivalentClass,
        OWL.equivalentProperty,
    ),
)
def test_structural_axioms_are_identical(predicate) -> None:
    assert _semantic_set(_baseline(), predicate) == _semantic_set(
        _modules(), predicate
    )


def test_critical_security_relations_have_one_authoritative_declaration() -> None:
    declarations: dict = {FIN.holds: [], FIN.securityIssuedBy: []}
    for relative in TEAM_V1_ONTOLOGY_FILES:
        graph = Graph().parse(ONTOLOGY_ROOT / relative, format="turtle")
        for relation in declarations:
            if (relation, RDF.type, OWL.ObjectProperty) in graph:
                declarations[relation].append(relative)

    assert declarations == {
        FIN.holds: ["common.ttl"],
        FIN.securityIssuedBy: ["common.ttl"],
    }
    graph = _modules()
    assert (FIN.holds, RDFS.domain, FIN.FinancialProduct) in graph
    assert (FIN.holds, RDFS.range, FIN.Security) in graph
    assert (FIN.securityIssuedBy, RDFS.domain, FIN.Security) in graph
    assert (FIN.securityIssuedBy, RDFS.range, FIN.Organization) in graph


def test_critical_class_uris_are_declared_in_module_union() -> None:
    graph = _modules()
    for resource in (
        FIN.FinancialProduct,
        FIN.Bond,
        FIN.ETF,
        FIN.ETN,
        FIN.Fund,
        FIN.FundShareClass,
        FIN.Security,
        FIN.EquitySecurity,
        FIN.Organization,
    ):
        assert (resource, RDF.type, OWL.Class) in graph


def test_shacl_results_match_before_and_after_split() -> None:
    before = _baseline()
    after = _modules()

    valid_before, valid_before_report = _conforms(
        before, _representative_data()
    )
    valid_after, valid_after_report = _conforms(
        after, _representative_data()
    )
    assert valid_before, valid_before_report
    assert valid_after, valid_after_report

    invalid_before, _ = _conforms(
        before, _representative_data(invalid_holding=True)
    )
    invalid_after, _ = _conforms(
        after, _representative_data(invalid_holding=True)
    )
    assert invalid_before is invalid_after is False


def test_team_loader_fails_closed_when_any_module_is_missing(tmp_path: Path) -> None:
    for relative in TEAM_V1_ONTOLOGY_FILES:
        if relative != "fund_pub.ttl":
            shutil.copy2(ONTOLOGY_ROOT / relative, tmp_path / relative)

    with pytest.raises(
        OntologyLoadError,
        match="missing mandatory ontology files:.*fund_pub.ttl",
    ):
        OntologyLoader(tmp_path, version="team-v1").load()


def test_team_loader_fails_closed_when_any_module_is_invalid(tmp_path: Path) -> None:
    for relative in TEAM_V1_ONTOLOGY_FILES:
        shutil.copy2(ONTOLOGY_ROOT / relative, tmp_path / relative)
    (tmp_path / "etf_gl.ttl").write_text(
        "this is not valid turtle", encoding="utf-8"
    )

    with pytest.raises(OntologyLoadError, match="failed to parse ontology"):
        OntologyLoader(tmp_path, version="team-v1").load()
