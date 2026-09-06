from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Storage(str, Enum):
    RDB = "rdb"
    GRAPH = "graph"
    VECTOR_BM25 = "vector_bm25"


@dataclass(frozen=True, slots=True)
class FieldStorageSpec:
    canonical_field: str
    ontology_property: str
    rdb_field_or_metric: str
    storages: tuple[Storage, ...]
    operations: frozenset[str]
    unit: str | None
    date_policy: str
    nullable: bool
    provenance_required: bool = True


FIELD_STORAGE_REGISTRY = {
    item.canonical_field: item
    for item in (
        FieldStorageSpec("product.aum", "hasObservation", "AUMObservation:du_last_aum|fd_nast_suma", (Storage.RDB,), frozenset({"range", "sort", "latest"}), "SOURCE_CURRENCY", "actual_observation_date", True),
        FieldStorageSpec("product.nav", "hasObservation", "NAVObservation:du_last_nav|du_bpr|bns_bpr", (Storage.RDB,), frozenset({"range", "sort", "latest"}), "SOURCE_CURRENCY", "actual_observation_date", True),
        FieldStorageSpec("product.price", "hasObservation", "PriceObservation", (Storage.RDB,), frozenset({"range", "sort", "latest"}), "SOURCE_CURRENCY", "actual_observation_date", True),
        FieldStorageSpec("product.return", "hasObservation", "ReturnObservation", (Storage.RDB,), frozenset({"project"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.one_day_return", "hasObservation", "ONE_DAY_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.one_month_return", "hasObservation", "ONE_MONTH_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.three_month_return", "hasObservation", "THREE_MONTH_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.six_month_return", "hasObservation", "SIX_MONTH_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.one_year_return", "hasObservation", "ONE_YEAR_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.year_to_date_return", "hasObservation", "YEAR_TO_DATE_RETURN", (Storage.RDB,), frozenset({"sort_contract", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.yield", "hasObservation", "YieldObservation", (Storage.RDB,), frozenset({"range", "sort", "latest"}), "PERCENT", "actual_observation_date", True),
        FieldStorageSpec("product.expense_ratio", "hasObservation", "FeeObservation", (Storage.RDB,), frozenset({"latest"}), None, "source_date_only", True),
        FieldStorageSpec("product.risk_grade", "hasRiskGrade", "canonical_v2.entity_classifications:RISK_GRADE", (Storage.RDB,), frozenset({"project"}), None, "snapshot", True),
        FieldStorageSpec("product.credit_rating", "hasCreditRating", "CreditRatingObservation", (Storage.RDB,), frozenset({"equality", "latest"}), None, "actual_observation_date", True),
        FieldStorageSpec("product.maturity", "maturityOrFirstCallDate", "bond_attributes.maturity_date", (Storage.RDB,), frozenset({"range", "sort"}), "DATE", "fixed_or_first_call", True),
        FieldStorageSpec("product.base_index", "tracks", "canonical_products.base_index", (Storage.RDB, Storage.GRAPH), frozenset({"equality", "relation_traversal"}), None, "snapshot", True),
        FieldStorageSpec("product.strategy_description", "strategyDescription", "etf_attributes.strategy", (Storage.VECTOR_BM25,), frozenset({"text_search"}), None, "snapshot", True),
        FieldStorageSpec("product.asset_manager", "managedBy", "product_relations:managedBy", (Storage.RDB, Storage.GRAPH), frozenset({"equality", "relation_traversal"}), None, "snapshot", True),
        FieldStorageSpec("product.issuer", "issuedBy", "product_relations:issuedBy", (Storage.RDB, Storage.GRAPH), frozenset({"equality", "relation_traversal"}), None, "snapshot", True),
    )
}
