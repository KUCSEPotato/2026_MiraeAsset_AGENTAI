from dataclasses import dataclass
from enum import Enum

from app.domain.models import QueryOperation, RetrievalSource


class FieldCapability(str, Enum):
    FILTER = "filter"
    SORT = "sort"
    PROJECT = "project"


@dataclass(frozen=True)
class CanonicalFieldMetadata:
    canonical_field: str
    preferred_sources: tuple[RetrievalSource, ...]
    supported_capabilities: frozenset[FieldCapability]


class RoutingMetadataRegistry:
    """Central M4 routing metadata, independent from dataset column names."""

    def __init__(self) -> None:
        all_capabilities = frozenset(
            {
                FieldCapability.FILTER,
                FieldCapability.SORT,
                FieldCapability.PROJECT,
            }
        )
        filter_and_project = frozenset(
            {FieldCapability.FILTER, FieldCapability.PROJECT}
        )
        self._fields = {
            "product.aum": CanonicalFieldMetadata(
                "product.aum",
                (RetrievalSource.RDB,),
                all_capabilities,
            ),
            "product.expense_ratio": CanonicalFieldMetadata(
                "product.expense_ratio",
                (RetrievalSource.RDB,),
                all_capabilities,
            ),
            "product.region": CanonicalFieldMetadata(
                "product.region",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.asset_type": CanonicalFieldMetadata(
                "product.asset_type",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.product_type": CanonicalFieldMetadata(
                "product.product_type",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.price": CanonicalFieldMetadata(
                "product.price",
                (RetrievalSource.RDB,),
                all_capabilities,
            ),
            "product.nav": CanonicalFieldMetadata(
                "product.nav",
                (RetrievalSource.RDB,),
                all_capabilities,
            ),
            "product.ticker": CanonicalFieldMetadata(
                "product.ticker",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.isin": CanonicalFieldMetadata(
                "product.isin",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.etp_distribution_status": CanonicalFieldMetadata(
                "product.etp_distribution_status",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.etp_trading_status": CanonicalFieldMetadata(
                "product.etp_trading_status",
                (RetrievalSource.RDB,),
                filter_and_project,
            ),
            "product.current_etp_sale_eligible": CanonicalFieldMetadata(
                "product.current_etp_sale_eligible",
                (RetrievalSource.RDB,),
                frozenset({FieldCapability.FILTER}),
            ),
            "product.latest_etp_price_available": CanonicalFieldMetadata(
                "product.latest_etp_price_available",
                (RetrievalSource.RDB,),
                frozenset({FieldCapability.FILTER}),
            ),
            "product.etp_listing_ended": CanonicalFieldMetadata(
                "product.etp_listing_ended",
                (RetrievalSource.RDB,),
                frozenset({FieldCapability.FILTER}),
            ),
            "product.stale_etp_price_warning": CanonicalFieldMetadata(
                "product.stale_etp_price_warning",
                (RetrievalSource.RDB,),
                frozenset({FieldCapability.FILTER}),
            ),
            "product.etp_insufficient_info": CanonicalFieldMetadata(
                "product.etp_insufficient_info",
                (RetrievalSource.RDB,),
                frozenset({FieldCapability.FILTER}),
            ),
            **{
                field: CanonicalFieldMetadata(
                    field,
                    (RetrievalSource.RDB,),
                    frozenset({FieldCapability.FILTER}),
                )
                for field in (
                    "product.current_sale_available",
                    "product.current_bond_purchase_eligible",
                    "product.bond_market_presence",
                    "product.has_sale_lot",
                    "product.has_multiple_sale_lots",
                    "product.has_trade_price_and_buy_yield_sale_lot",
                )
            },
        }
        self._source_operations = {
            RetrievalSource.RDB: frozenset(
                {
                    QueryOperation.SEARCH_PRODUCTS,
                    QueryOperation.FILTER_CANDIDATES,
                    QueryOperation.RANK_CANDIDATES,
                }
            ),
            RetrievalSource.GRAPH: frozenset(
                {QueryOperation.RELATIONSHIP_SEARCH}
            ),
            RetrievalSource.VECTOR: frozenset({QueryOperation.SEMANTIC_SEARCH}),
            RetrievalSource.BM25: frozenset({QueryOperation.SEMANTIC_SEARCH}),
            RetrievalSource.INTERNAL: frozenset(
                {
                    QueryOperation.FILTER_CANDIDATES,
                    QueryOperation.RANK_CANDIDATES,
                }
            ),
        }

    def field(self, canonical_field: str) -> CanonicalFieldMetadata | None:
        return self._fields.get(canonical_field)

    def supports_field(
        self,
        canonical_field: str,
        capability: FieldCapability,
    ) -> bool:
        metadata = self.field(canonical_field)
        return metadata is not None and capability in metadata.supported_capabilities

    def supports_operation(
        self,
        source: RetrievalSource,
        operation: QueryOperation,
    ) -> bool:
        return operation in self._source_operations.get(source, frozenset())
