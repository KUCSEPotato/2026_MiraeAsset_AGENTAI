"""Generate and verify the five submission ontology modules.

The merged Team Ontology remains the immutable semantic baseline.  This tool
partitions its triples by named subject, keeps each blank-node subgraph with its
owning subject, and proves that parsing the five outputs produces an RDF graph
isomorphic to the baseline.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from rdflib import BNode, Graph, URIRef
from rdflib.compare import isomorphic, to_canonical_graph


ROOT = Path(__file__).resolve().parents[1]
ONTOLOGY_ROOT = ROOT / "ontology"
BASELINE = ONTOLOGY_ROOT / "candidates" / "new_optical_ontology.ttl"
MODULES = {
    "common": ONTOLOGY_ROOT / "common.ttl",
    "bond_kr": ONTOLOGY_ROOT / "bond_kr.ttl",
    "etf_kr": ONTOLOGY_ROOT / "etf_kr.ttl",
    "etf_gl": ONTOLOGY_ROOT / "etf_gl.ttl",
    "fund_pub": ONTOLOGY_ROOT / "fund_pub.ttl",
}

BOND_SUBJECTS = {
    "Bond",
    "BondType",
    "CreditRating",
    "InterestPaymentType",
    "InterestRateType",
    "SaleLot",
    "TradingChannel",
    "TradingType",
    "SaleLotShape",
    "availableThroughTradingChannel",
    "hasBondType",
    "hasCreditRating",
    "hasInstrumentCountry",
    "hasInterestPaymentType",
    "hasInterestRateType",
    "hasSaleLot",
    "hasTradingType",
    "issueDate",
    "lotSequence",
    "maturityOrFirstCallDate",
    "totalIssueAmount",
}

ETF_KR_SUBJECTS = {
    "ETF",
    "ETN",
    "ExchangeTradedProduct",
    "ETFShape",
    "ETNShape",
    "IndexExposureMethod",
    "ManagementStyle",
    "ReplicationMethod",
    "hasIndexExposureMethod",
    "hasManagementStyle",
    "hasReplicationMethod",
    "hasUnderlyingIndex",
    "leverageFactor",
    "tracksIndex",
}

ETF_GL_SUBJECTS = {
    "Exchange",
    "Market",
    "investmentStrategyDescription",
    "listedInCountry",
    "listedInMarket",
    "listedOnExchange",
    "tradedInCurrency",
}

FUND_SUBJECTS = {
    "Fund",
    "FundAttribute",
    "FundClassification",
    "FundSetupType",
    "FundShareClass",
    "FundShareClassShape",
    "InvestorType",
    "ManagementAttribute",
    "SalesChannel",
    "ShareClassFeeType",
    "Trustee",
    "availableThroughSalesChannel",
    "subscriptionStatus",
    "isSoldByMiraeAsset",
    "SubscriptionStatus",
    "OPEN_FOR_SUBSCRIPTION",
    "CLOSED_FOR_SUBSCRIPTION",
    "classificationScheme",
    "establishedInCountry",
    "hasBenchmark",
    "hasFundAttribute",
    "hasFundClassification",
    "hasFundSetupType",
    "hasInvestorType",
    "hasManagementAttribute",
    "hasShareClass",
    "hasShareClassFeeType",
    "hasTrustee",
}


def local_name(resource: URIRef) -> str:
    value = str(resource)
    return value.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def module_for(subject: URIRef) -> str:
    name = local_name(subject)
    if name.startswith("BOND_TYPE_") or name in BOND_SUBJECTS:
        return "bond_kr"
    if name in ETF_KR_SUBJECTS:
        return "etf_kr"
    if name in ETF_GL_SUBJECTS:
        return "etf_gl"
    if name in FUND_SUBJECTS:
        return "fund_pub"
    return "common"


def partition(source: Graph) -> dict[str, Graph]:
    result = {name: Graph() for name in MODULES}
    for graph in result.values():
        for prefix, namespace in source.namespaces():
            graph.bind(prefix, namespace)

    blank_owners: dict[BNode, set[str]] = defaultdict(set)

    def assign_blank(root: BNode, module: str) -> None:
        pending = [root]
        visited: set[BNode] = set()
        while pending:
            subject = pending.pop()
            if subject in visited:
                continue
            visited.add(subject)
            blank_owners[subject].add(module)
            for triple in source.triples((subject, None, None)):
                result[module].add(triple)
                if isinstance(triple[2], BNode):
                    pending.append(triple[2])

    for subject in sorted(
        {item for item in source.subjects() if isinstance(item, URIRef)},
        key=str,
    ):
        module = module_for(subject)
        for triple in source.triples((subject, None, None)):
            result[module].add(triple)
            if isinstance(triple[2], BNode):
                assign_blank(triple[2], module)

    shared_blanks = {
        str(node): sorted(owners)
        for node, owners in blank_owners.items()
        if len(owners) > 1
    }
    if shared_blanks:
        raise ValueError(f"blank-node subgraph crosses module boundaries: {shared_blanks}")

    assigned = Graph()
    for graph in result.values():
        assigned += graph
    if not isomorphic(source, assigned):
        raise ValueError("in-memory module union is not isomorphic to baseline")
    return result


def parsed_union(paths=MODULES.values()) -> Graph:
    graph = Graph()
    for path in paths:
        graph.parse(path, format="turtle")
    return graph


def verify() -> tuple[int, dict[str, int]]:
    baseline = Graph().parse(BASELINE, format="turtle")
    modules = parsed_union()
    if not isomorphic(baseline, modules):
        raise ValueError("submission module union is not isomorphic to merged baseline")
    return len(baseline), {
        name: len(Graph().parse(path, format="turtle"))
        for name, path in MODULES.items()
    }


def write_modules() -> None:
    source = Graph().parse(BASELINE, format="turtle")
    for name, graph in partition(source).items():
        canonical = Graph()
        for prefix, namespace in source.namespaces():
            canonical.bind(prefix, namespace)
        canonical += to_canonical_graph(graph)
        serialized = canonical.serialize(format="turtle")
        body = (
            "\n".join(
                line.rstrip() for line in serialized.rstrip().splitlines()
            )
            + "\n"
        )
        header = (
            "# Generated from candidates/new_optical_ontology.ttl by "
            "scripts/modularize_ontology.py.\n"
            "# Do not edit semantics in this module independently of the merged baseline.\n\n"
        )
        MODULES[name].write_text(header + body, encoding="utf-8")
    verify()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the five modules before verifying equivalence",
    )
    args = parser.parse_args()
    if args.write:
        write_modules()
    triples, counts = verify()
    print(f"ontology_modules_ok baseline_triples={triples} modules={counts}")


if __name__ == "__main__":
    main()
