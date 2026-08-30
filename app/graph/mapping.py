from dataclasses import dataclass

from app.ontology.index import FP, OntologyIndex
from app.ontology.models import OntologyLoadError

LEGACY_RUNTIME_MAPPING_FILE = "mappings/column_mapping.csv"
V7_RUNTIME_MAPPING_FILE = "mappings/v7_runtime_mapping.csv"
TEAM_V1_RUNTIME_MAPPING_FILE = "runtime:team-v1-runtime-2026-08-29"


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
    additional_subject_types: tuple[str, ...] = ()
    additional_object_types: tuple[str, ...] = ()


class GraphMappingRegistry:
    """One allow-list for ontology relations, Cypher edges and node types."""

    def __init__(
        self,
        ontology_index: OntologyIndex | None = None,
        *,
        version: str = "legacy",
    ) -> None:
        normalized = "team-v1" if version == "team_v1" else version
        if normalized not in {"legacy", "v7", "team-v1", "canonical-v2"}:
            raise ValueError(
                "graph mapping version must be 'legacy', 'v7', 'team-v1', or 'canonical-v2'"
            )
        mappings = {
            "legacy": _legacy_mappings,
            "v7": _v7_mappings,
            "team-v1": _team_v1_mappings,
            "canonical-v2": _canonical_v2_mappings,
        }[normalized]()
        self.version = normalized
        self.runtime_mapping_file = (
            "runtime:canonical-v2-derived-store-m10.8-d" if normalized == "canonical-v2" else
            TEAM_V1_RUNTIME_MAPPING_FILE if normalized == "team-v1" else
            V7_RUNTIME_MAPPING_FILE if normalized == "v7" else
            LEGACY_RUNTIME_MAPPING_FILE
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
        # canonical_v2 relation admission is already enforced by its audited
        # PostgreSQL domain contract.  The graph projection must not invent a
        # second mapping from source labels while the Team TTL evolves.
        if self.version == "canonical-v2":
            return
        for mapping in self.mappings:
            if mapping.ontology_uri not in ontology_index.object_properties:
                raise OntologyLoadError(
                    f"graph relation is absent from ontology: {mapping.ontology_uri}"
                )
            subject_types = (
                mapping.subject_type,
                *mapping.additional_subject_types,
            )
            object_types = (
                mapping.object_type,
                *mapping.additional_object_types,
            )
            if not all(
                ontology_index.is_compatible(
                    subject_type,
                    mapping.canonical_relation,
                    object_type,
                )
                for subject_type in subject_types
                for object_type in object_types
            ):
                raise OntologyLoadError(
                    "graph mapping violates ontology domain/range: "
                    f"{mapping.canonical_relation}"
                )


def _legacy_mappings() -> tuple[GraphRelationMapping, ...]:
    return (
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


def _team_v1_mappings() -> tuple[GraphRelationMapping, ...]:
    return (
        GraphRelationMapping(
            "managedBy", str(FP.managedBy), "MANAGED_BY",
            "ETF", "Organization",
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
            ("Fund",),
            ("AssetManagementCompany",),
        ),
        GraphRelationMapping(
            "issuedBy", str(FP.issuedBy), "ISSUED_BY",
            "Bond", "Organization",
            (
                GraphSourceBinding(
                    "domestic_bond", "canonical_products.issuer"
                ),
            ),
        ),
        GraphRelationMapping(
            "tracksIndex", str(FP.tracksIndex), "TRACKS_INDEX",
            "ExchangeTradedProduct", "Index",
            (
                GraphSourceBinding(
                    "foreign_etf", "product_relations.tracksIndex"
                ),
            ),
        ),
        GraphRelationMapping(
            "hasUnderlyingIndex", str(FP.hasUnderlyingIndex),
            "HAS_UNDERLYING_INDEX", "ExchangeTradedProduct", "Index",
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
            "hasShareClass", str(FP.hasShareClass), "HAS_SHARE_CLASS",
            "Fund", "FundShareClass",
            (
                GraphSourceBinding("public_fund", "fund_classes.fund_id"),
            ),
        ),
        GraphRelationMapping(
            "hasBenchmark", str(FP.hasBenchmark), "HAS_BENCHMARK",
            "Fund", "Index",
            (GraphSourceBinding("public_fund", "canonical_products.base_index"),),
        ),
        GraphRelationMapping(
            "denominatedIn", str(FP.denominatedIn), "DENOMINATED_IN",
            "FinancialProduct", "Currency",
            (GraphSourceBinding("domestic_bond", "canonical_products.currency"),),
        ),
        GraphRelationMapping(
            "hasRiskGrade", str(FP.hasRiskGrade), "HAS_RISK_GRADE",
            "FinancialProduct", "RiskGrade",
            (
                GraphSourceBinding("domestic_bond", "canonical_products.risk_grade"),
                GraphSourceBinding("domestic_etf", "canonical_products.risk_grade"),
                GraphSourceBinding("public_fund", "canonical_products.risk_grade"),
            ),
            ("FundShareClass",),
        ),
        GraphRelationMapping(
            "hasAssetClass", str(FP.hasAssetClass), "HAS_ASSET_CLASS",
            "FinancialProduct", "AssetClass",
            (
                GraphSourceBinding("domestic_etf", "canonical_products.asset_type"),
                GraphSourceBinding("foreign_etf", "canonical_products.asset_type"),
                GraphSourceBinding("public_fund", "canonical_products.asset_type"),
            ),
            ("FundShareClass",),
        ),
        GraphRelationMapping(
            "hasExposureRegion", str(FP.hasExposureRegion),
            "HAS_EXPOSURE_REGION", "FinancialProduct", "ExposureRegion",
            (
                GraphSourceBinding("domestic_etf", "canonical_products.region"),
                GraphSourceBinding("foreign_etf", "canonical_products.region"),
                GraphSourceBinding("public_fund", "canonical_products.region"),
            ),
            ("FundShareClass",),
        ),
        GraphRelationMapping(
            "hasMarketScope", str(FP.hasMarketScope),
            "HAS_MARKET_SCOPE", "FundShareClass", "MarketScope",
            (
                GraphSourceBinding(
                    "public_fund", "source_public_funds.payload.ovrs_fd_desc"
                ),
            ),
        ),
        GraphRelationMapping(
            "tradedInCurrency", str(FP.tradedInCurrency),
            "TRADED_IN_CURRENCY", "ExchangeTradedProduct", "Currency",
            (
                GraphSourceBinding("domestic_etf", "canonical_products.currency"),
                GraphSourceBinding("foreign_etf", "canonical_products.currency"),
            ),
        ),
        GraphRelationMapping(
            "hasOfferingType", str(FP.hasOfferingType),
            "HAS_OFFERING_TYPE", "FundShareClass", "OfferingType",
            (GraphSourceBinding("public_fund", "fund_classes.public_private"),),
            ("Bond",),
        ),
        GraphRelationMapping(
            "hasSaleLot", str(FP.hasSaleLot), "HAS_SALE_LOT",
            "Bond", "SaleLot",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.source_record_key"),),
        ),
        GraphRelationMapping(
            "hasBondType", str(FP.hasBondType), "HAS_BOND_TYPE",
            "Bond", "BondType",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.bd_knd"),),
        ),
        GraphRelationMapping(
            "hasInterestRateType", str(FP.hasInterestRateType),
            "HAS_INTEREST_RATE_TYPE", "Bond", "InterestRateType",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.bd_inrt_tcd"),),
        ),
        GraphRelationMapping(
            "hasInterestPaymentType", str(FP.hasInterestPaymentType),
            "HAS_INTEREST_PAYMENT_TYPE", "Bond", "InterestPaymentType",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.bd_intp_tcd"),),
        ),
        GraphRelationMapping(
            "hasCreditRating", str(FP.hasCreditRating),
            "HAS_CREDIT_RATING", "Bond", "CreditRating",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.crd_grd"),),
        ),
        GraphRelationMapping(
            "hasTradingType", str(FP.hasTradingType), "HAS_TRADING_TYPE",
            "SaleLot", "TradingType",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.pd_exg_mkt"),),
        ),
        GraphRelationMapping(
            "availableThroughTradingChannel",
            str(FP.availableThroughTradingChannel),
            "AVAILABLE_THROUGH_TRADING_CHANNEL",
            "SaleLot", "TradingChannel",
            (GraphSourceBinding("domestic_bond", "source_domestic_bonds.payload.bdbns_abl_chnl_nm"),),
        ),
    )


def _v7_mappings() -> tuple[GraphRelationMapping, ...]:
    return (
        GraphRelationMapping(
            "managedBy", str(FP.managedBy), "MANAGED_BY",
            "ETF", "AssetManagementCompany",
            (
                GraphSourceBinding(
                    "domestic_etp", "canonical_products.asset_manager"
                ),
                GraphSourceBinding(
                    "foreign_etp", "canonical_products.asset_manager"
                ),
            ),
        ),
        GraphRelationMapping(
            "tracksIndex", str(FP.tracksIndex), "TRACKS_INDEX",
            "ETF", "Index",
            (
                GraphSourceBinding(
                    "domestic_etp", "canonical_products.base_index"
                ),
                GraphSourceBinding(
                    "foreign_etp", "canonical_products.base_index"
                ),
            ),
        ),
        GraphRelationMapping(
            "hasShareClass", str(FP.hasShareClass), "HAS_SHARE_CLASS",
            "Fund", "FundShareClass",
            (GraphSourceBinding("public_fund", "fund_classes.fund_id"),),
        ),
        GraphRelationMapping(
            "hasSaleLot", str(FP.hasSaleLot), "HAS_SALE_LOT",
            "Bond", "SaleLot", (),
        ),
    )


def _canonical_v2_mappings() -> tuple[GraphRelationMapping, ...]:
    """The sole Cypher allow-list for canonical_v2 relation facts."""
    items = (
        ("hasShareClass", "HAS_SHARE_CLASS"),
        ("hasSaleLot", "HAS_SALE_LOT"),
        ("managedBy", "MANAGED_BY"),
        ("issuedBy", "ISSUED_BY"),
        ("hasTrustee", "HAS_TRUSTEE"),
        ("hasUnderlyingIndex", "HAS_UNDERLYING_INDEX"),
        ("tracksIndex", "TRACKS_INDEX"),
        ("hasBenchmark", "HAS_BENCHMARK"),
        ("denominatedIn", "DENOMINATED_IN"),
        ("tradedInCurrency", "TRADED_IN_CURRENCY"),
        ("listedInCountry", "LISTED_IN_COUNTRY"),
        ("hasInstrumentCountry", "HAS_INSTRUMENT_COUNTRY"),
        ("hasAssetClass", "HAS_ASSET_CLASS"),
        ("hasExposureRegion", "HAS_EXPOSURE_REGION"),
        ("hasMarketScope", "HAS_MARKET_SCOPE"),
        ("hasRiskGrade", "HAS_RISK_GRADE"),
        ("hasBondType", "HAS_BOND_TYPE"),
        ("hasOfferingType", "HAS_OFFERING_TYPE"),
        ("holds", "HOLDS"),
        ("securityIssuedBy", "SECURITY_ISSUED_BY"),
    )
    return tuple(
        GraphRelationMapping(
            canonical_relation=name,
            ontology_uri=str(getattr(FP, name)),
            edge_type=edge,
            subject_type="SemanticEntity",
            object_type="SemanticEntity",
            source_bindings=(),
        )
        for name, edge in items
    )


GRAPH_NODE_LABELS = frozenset(
    {
        "M10Entity",
        "FinancialProduct",
        "ETF",
        "ETN",
        "Bond",
        "Fund",
        "FundShareClass",
        "SaleLot",
        "FundClass",
        "AssetManager",
        "AssetManagementCompany",
        "Issuer",
        "SecuritiesCompany",
        "Index",
        "Benchmark",
        "Region",
        "AssetType",
        "RiskGrade",
        "Currency",
        "Organization",
        "AssetClass",
        "ExposureRegion",
        "MarketScope",
        "OfferingType",
        "BondType",
        "InterestRateType",
        "InterestPaymentType",
        "CreditRating",
        "TradingType",
        "TradingChannel",
        "M108DNode",
    }
)
