from dataclasses import dataclass

from app.ontology.index import FP, OntologyIndex
from app.ontology.models import OntologyLoadError


@dataclass(frozen=True, slots=True)
class GraphSourceBinding:
    source_dataset: str
    source_field: str


@dataclass(frozen=True, slots=True)
class GraphRelationMapping:
    canonical_relation: str
    ontology_uri: str
    edge_type: str
    subject_type: str
    object_type: str
    source_bindings: tuple[GraphSourceBinding, ...]


class GraphMappingRegistry:
    """One allow-list for ontology relations, Cypher edges and node types."""

    def __init__(self, ontology_index: OntologyIndex | None = None) -> None:
        mappings = (
            GraphRelationMapping(
                "managedBy", str(FP.managedBy), "MANAGED_BY",
                "FinancialProduct", "AssetManager",
                (
                    GraphSourceBinding(
                        "domestic_etf", "canonical_products.asset_manager"
                    ),
                    GraphSourceBinding(
                        "foreign_etf", "canonical_products.asset_manager"
                    ),
                    GraphSourceBinding(
                        "public_fund", "canonical_products.issuer"
                    ),
                ),
            ),
            GraphRelationMapping(
                "issuedBy", str(FP.issuedBy), "ISSUED_BY", "Bond", "Issuer",
                (
                    GraphSourceBinding(
                        "domestic_bond", "canonical_products.issuer"
                    ),
                ),
            ),
            GraphRelationMapping(
                "tracks", str(FP.tracks), "TRACKS",
                "ExchangeTradedProduct", "Index",
                (
                    GraphSourceBinding(
                        "domestic_etf", "canonical_products.base_index"
                    ),
                    GraphSourceBinding(
                        "foreign_etf", "canonical_products.base_index"
                    ),
                ),
            ),
            GraphRelationMapping(
                "referencesBenchmark", str(FP.referencesBenchmark),
                "REFERENCES_BENCHMARK", "Fund", "Benchmark",
                (
                    GraphSourceBinding(
                        "public_fund", "canonical_products.base_index"
                    ),
                ),
            ),
            GraphRelationMapping(
                "hasClass", str(FP.hasClass), "HAS_CLASS", "Fund", "FundClass",
                (
                    GraphSourceBinding("public_fund", "fund_classes.fund_id"),
                ),
            ),
            GraphRelationMapping(
                "investsInRegion", str(FP.investsInRegion),
                "INVESTS_IN_REGION", "FinancialProduct", "Region",
                (
                    GraphSourceBinding(
                        "domestic_etf", "canonical_products.region"
                    ),
                    GraphSourceBinding(
                        "foreign_etf", "canonical_products.region"
                    ),
                ),
            ),
            GraphRelationMapping(
                "hasAssetType", str(FP.hasAssetType), "HAS_ASSET_TYPE",
                "FinancialProduct", "AssetType",
                (
                    GraphSourceBinding(
                        "domestic_etf", "canonical_products.asset_type"
                    ),
                    GraphSourceBinding(
                        "foreign_etf", "canonical_products.asset_type"
                    ),
                ),
            ),
            GraphRelationMapping(
                "hasRiskGrade", str(FP.hasRiskGrade), "HAS_RISK_GRADE",
                "FinancialProduct", "RiskGrade",
                (
                    GraphSourceBinding(
                        "domestic_etf", "canonical_products.risk_grade"
                    ),
                    GraphSourceBinding(
                        "domestic_bond", "canonical_products.risk_grade"
                    ),
                ),
            ),
            GraphRelationMapping(
                "denominatedIn", str(FP.denominatedIn), "DENOMINATED_IN",
                "FinancialProduct", "Currency",
                (
                    GraphSourceBinding(
                        "domestic_bond", "canonical_products.currency"
                    ),
                ),
            ),
        )
        self._by_relation = {item.canonical_relation: item for item in mappings}
        self._by_uri = {item.ontology_uri: item for item in mappings}
        self._by_edge = {item.edge_type: item for item in mappings}
        if ontology_index is not None:
            self.validate_ontology(ontology_index)

    @property
    def mappings(self) -> tuple[GraphRelationMapping, ...]:
        return tuple(self._by_relation.values())

    @property
    def edge_types(self) -> frozenset[str]:
        return frozenset(self._by_edge)

    def get(self, relation: str) -> GraphRelationMapping:
        mapping = self._by_relation.get(relation) or self._by_uri.get(relation)
        if mapping is None:
            raise ValueError(f"unsupported graph relation: {relation}")
        return mapping

    def by_edge(self, edge_type: str) -> GraphRelationMapping:
        mapping = self._by_edge.get(edge_type)
        if mapping is None:
            raise ValueError(f"unsupported graph edge type: {edge_type}")
        return mapping

    def validate_ontology(self, ontology_index: OntologyIndex) -> None:
        for mapping in self.mappings:
            if mapping.ontology_uri not in ontology_index.object_properties:
                raise OntologyLoadError(
                    f"graph relation is absent from ontology: {mapping.ontology_uri}"
                )
            if not ontology_index.is_compatible(
                mapping.subject_type,
                mapping.canonical_relation,
                mapping.object_type,
            ):
                raise OntologyLoadError(
                    "graph mapping violates ontology domain/range: "
                    f"{mapping.canonical_relation}"
                )


GRAPH_NODE_LABELS = frozenset(
    {
        "M10Entity",
        "FinancialProduct",
        "ETF",
        "ETN",
        "Bond",
        "Fund",
        "FundClass",
        "AssetManager",
        "Issuer",
        "Index",
        "Benchmark",
        "Region",
        "AssetType",
        "RiskGrade",
        "Currency",
    }
)
