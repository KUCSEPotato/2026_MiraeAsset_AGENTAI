from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph, Namespace, OWL, RDF, RDFS


ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_DIR = ROOT / "ontology"

ONTOLOGY_FILES = [
    "common.ttl",
    "bond_kr.ttl",
    "etf_kr.ttl",
    "etf_gl.ttl",
    "fund_pub.ttl",
]

FIN = Namespace("https://miraeasset.com/ontology/financial-product#")

REQUIRED_TERMS = {
    "common.ttl": [
        FIN.FinancialProduct,
        FIN.DebtSecurity,
        FIN.FundProduct,
        FIN.Organization,
        FIN.AssetManagementCompany,
        FIN.Currency,
        FIN.RiskGrade,
        FIN.Identifier,
        FIN.IdentifierScheme,
        FIN.Index,
        FIN.ClassificationConcept,
        FIN.SourceDataset,
    ],
    "bond_kr.ttl": [
        FIN.Bond,
        FIN.BondType,
        FIN.CreditRating,
        FIN.InterestPaymentType,
        FIN.InterestRateType,
        FIN.TradingChannel,
        FIN.TradingType,
        FIN.SaleLot,
        FIN.hasSaleLot,
    ],
    "etf_kr.ttl": [
        FIN.ExchangeTradedProduct,
        FIN.ETF,
        FIN.ETN,
        FIN.ManagementStyle,
        FIN.ReplicationMethod,
        FIN.IndexExposureMethod,
        FIN.tracksIndex,
        FIN.hasUnderlyingIndex,
    ],
    "etf_gl.ttl": [
        FIN.Exchange,
        FIN.Market,
        FIN.listedOnExchange,
        FIN.listedInMarket,
        FIN.tradedInCurrency,
        FIN.investmentStrategyDescription,
    ],
    "fund_pub.ttl": [
        FIN.Fund,
        FIN.FundShareClass,
        FIN.FundClassification,
        FIN.FundSetupType,
        FIN.InvestorType,
        FIN.SalesChannel,
        FIN.Trustee,
        FIN.hasShareClass,
    ],
}


def short(uri: object) -> str:
    text = str(uri)
    base = str(FIN)
    return f"fin:{text[len(base):]}" if text.startswith(base) else text


def parse_all() -> tuple[Graph, dict[str, Graph], list[str]]:
    merged = Graph()
    by_file: dict[str, Graph] = {}
    errors: list[str] = []

    for filename in ONTOLOGY_FILES:
        path = ONTOLOGY_DIR / filename
        if not path.exists():
            errors.append(f"필수 파일 없음: ontology/{filename}")
            continue

        graph = Graph()
        try:
            graph.parse(path, format="turtle")
        except Exception as exc:
            errors.append(f"Turtle 파싱 실패: ontology/{filename} -> {exc}")
            continue

        by_file[filename] = graph
        for triple in graph:
            merged.add(triple)

    return merged, by_file, errors


def validate_required_terms(by_file: dict[str, Graph]) -> list[str]:
    errors: list[str] = []

    for filename, terms in REQUIRED_TERMS.items():
        graph = by_file.get(filename)
        if graph is None:
            continue

        subjects = set(graph.subjects())
        for term in terms:
            if term not in subjects:
                errors.append(
                    f"필수 용어 누락: {short(term)} 가 ontology/{filename}에 정의되어 있지 않음"
                )

    return errors


def validate_internal_references(graph: Graph) -> list[str]:
    subjects = {subject for subject, _, _ in graph if str(subject).startswith(str(FIN))}
    references = {
        node
        for _, predicate, object_ in graph
        for node in (predicate, object_)
        if str(node).startswith(str(FIN))
    }

    unresolved = sorted(references - subjects, key=str)
    return [f"정의되지 않은 내부 참조: {short(term)}" for term in unresolved]


def validate_class_links(graph: Graph) -> list[str]:
    declared_classes = set(graph.subjects(RDF.type, OWL.Class))
    errors: list[str] = []

    for predicate in (RDFS.subClassOf, RDFS.domain, RDFS.range):
        for subject, target in graph.subject_objects(predicate):
            if str(target).startswith(str(FIN)) and target not in declared_classes:
                errors.append(
                    f"Class 참조 오류: {short(subject)} -- {predicate.n3()} --> "
                    f"{short(target)} (owl:Class 선언 없음)"
                )

    return errors


def report_summary(merged: Graph, by_file: dict[str, Graph]) -> None:
    print("[Ontology validation summary]")
    for filename in ONTOLOGY_FILES:
        graph = by_file.get(filename)
        if graph is not None:
            print(f"- {filename}: {len(graph)} triples")

    print(f"- merged: {len(merged)} triples")
    print(f"- classes: {len(set(merged.subjects(RDF.type, OWL.Class)))}")
    print(f"- object properties: {len(set(merged.subjects(RDF.type, OWL.ObjectProperty)))}")
    print(f"- datatype properties: {len(set(merged.subjects(RDF.type, OWL.DatatypeProperty)))}")


def main() -> int:
    merged, by_file, errors = parse_all()
    errors.extend(validate_required_terms(by_file))
    errors.extend(validate_internal_references(merged))
    errors.extend(validate_class_links(merged))

    report_summary(merged, by_file)

    if errors:
        print("\n[FAIL]")
        for error in errors:
            print(f"- {error}")
        return 1

    print("\n[PASS] 5개 Turtle 파일의 파싱 및 기본 구조 검증이 완료되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
