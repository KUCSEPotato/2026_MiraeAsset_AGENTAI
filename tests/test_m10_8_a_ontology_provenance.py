from pathlib import Path

from pyshacl import validate
from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, SH, XSD

from app.ontology.loader import OntologyLoader
from app.ontology.runtime_mapping import DATASET_SNAPSHOT, ONTOLOGY_URI


FIN = Namespace(f"{ONTOLOGY_URI}#")
EX = Namespace("https://miraeasset.com/data/m10-8-a-test/")


def _add_product(
    data: Graph,
    *,
    resource,
    product_type,
    identifier_value: str,
) -> None:
    identifier = EX[f"identifier-{identifier_value}"]
    data.add((resource, RDF.type, product_type))
    data.add((resource, FIN.internalProductID, Literal(identifier_value)))
    data.add((resource, FIN.productName, Literal(f"Product {identifier_value}")))
    data.add((resource, FIN.hasIdentifier, identifier))
    data.add((identifier, RDF.type, FIN.Identifier))
    data.add((identifier, FIN.identifierValue, Literal(identifier_value)))
    data.add((identifier, FIN.identifierScheme, FIN.SOURCE_ID))
    data.add((identifier, FIN.identifierNamespace, Literal("m10.8-a-test")))
    data.add((identifier, FIN.validationStatus, Literal("VALIDATED")))


def _add_record(data: Graph, *, resource, primary_key: str) -> None:
    data.add((resource, RDF.type, FIN.SourceRecord))
    data.add((resource, FIN.sourcePrimaryKey, Literal(primary_key)))
    data.add((resource, FIN.sourceRowNumber, Literal(2, datatype=XSD.positiveInteger)))
    data.add((resource, FIN.snapshotDate, Literal(DATASET_SNAPSHOT, datatype=XSD.date)))
    data.add((resource, FIN.inDataset, EX.dataset))


def _add_primary_product_record(
    data: Graph,
    *,
    product,
    record,
    primary_key: str,
) -> None:
    _add_record(data, resource=record, primary_key=primary_key)
    data.add((record, FIN.describesProduct, product))
    data.add((product, FIN.hasSourceRecord, record))


def test_team_ontology_uses_primary_and_supporting_entity_provenance() -> None:
    ontology = OntologyLoader(Path("ontology"), version="team-v1").load().graph
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))

    _add_product(
        data,
        resource=EX.bond,
        product_type=FIN.Bond,
        identifier_value="BOND-1",
    )
    _add_primary_product_record(
        data,
        product=EX.bond,
        record=EX.bond_primary_record,
        primary_key="BOND-PRIMARY-1",
    )
    data.add((EX.sale_lot, RDF.type, FIN.SaleLot))
    data.add((EX.bond, FIN.hasSaleLot, EX.sale_lot))
    _add_record(data, resource=EX.prbd_record, primary_key="PRBD-1")
    data.add((EX.prbd_record, FIN.describesEntity, EX.sale_lot))
    data.add((EX.prbd_record, FIN.supportsEntity, EX.bond))

    _add_product(
        data,
        resource=EX.fund,
        product_type=FIN.Fund,
        identifier_value="FUND-1",
    )
    _add_primary_product_record(
        data,
        product=EX.fund,
        record=EX.fund_primary_record,
        primary_key="FUND-PRIMARY-1",
    )
    data.add((EX.fund_share_class, RDF.type, FIN.FundShareClass))
    data.add((EX.fund, FIN.hasShareClass, EX.fund_share_class))
    _add_record(data, resource=EX.prfd_record, primary_key="PRFD-1")
    data.add((EX.prfd_record, FIN.describesEntity, EX.fund_share_class))
    data.add((EX.prfd_record, FIN.supportsEntity, EX.fund))

    assert set(data.objects(EX.prbd_record, FIN.describesEntity)) == {
        EX.sale_lot
    }
    assert EX.bond in data.objects(EX.prbd_record, FIN.supportsEntity)
    assert set(data.objects(EX.prfd_record, FIN.describesEntity)) == {
        EX.fund_share_class
    }
    assert EX.fund in data.objects(EX.prfd_record, FIN.supportsEntity)

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )

    assert conforms, str(report)


def test_source_record_shape_requires_exactly_one_described_entity() -> None:
    ontology = OntologyLoader(Path("ontology"), version="team-v1").load().graph
    property_shapes = {
        shape
        for shape in ontology.objects(FIN.SourceRecordShape, SH.property)
        if (shape, SH.path, FIN.describesEntity) in ontology
    }

    assert len(property_shapes) == 1
    shape = property_shapes.pop()
    assert (shape, SH.minCount, Literal(1)) in ontology
    assert (shape, SH.maxCount, Literal(1)) in ontology


def test_two_described_entities_fail_shacl() -> None:
    ontology = OntologyLoader(Path("ontology"), version="team-v1").load().graph
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))
    _add_record(data, resource=EX.invalid_record, primary_key="INVALID-1")
    data.add((EX.primary_a, RDF.type, FIN.EvidenceBearingEntity))
    data.add((EX.primary_b, RDF.type, FIN.EvidenceBearingEntity))
    data.add((EX.invalid_record, FIN.describesEntity, EX.primary_a))
    data.add((EX.invalid_record, FIN.describesEntity, EX.primary_b))

    conforms, _, _ = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )

    assert not conforms


def test_multiple_supporting_entities_are_allowed_and_independent() -> None:
    ontology = OntologyLoader(Path("ontology"), version="team-v1").load().graph
    data = Graph()
    data.add((EX.dataset, RDF.type, FIN.SourceDataset))
    _add_record(data, resource=EX.support_record, primary_key="SUPPORT-1")
    for resource in (EX.primary, EX.support_a, EX.support_b):
        data.add((resource, RDF.type, FIN.EvidenceBearingEntity))
    data.add((EX.support_record, FIN.describesEntity, EX.primary))
    data.add((EX.support_record, FIN.supportsEntity, EX.support_a))
    data.add((EX.support_record, FIN.supportsEntity, EX.support_b))

    conforms, _, report = validate(
        data_graph=data,
        shacl_graph=ontology,
        ont_graph=ontology,
        inference="rdfs",
        advanced=True,
        abort_on_first=False,
    )

    assert conforms, str(report)
    assert (FIN.supportsEntity, RDF.type, OWL.ObjectProperty) in ontology
    assert (FIN.supportsEntity, RDFS.domain, FIN.SourceRecord) in ontology
    assert (
        FIN.supportsEntity,
        RDFS.range,
        FIN.EvidenceBearingEntity,
    ) in ontology
    assert (
        FIN.supportsEntity,
        RDFS.subPropertyOf,
        FIN.describesEntity,
    ) not in ontology
