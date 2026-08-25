from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)


metadata = MetaData()


def _source_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("source_dataset", String(32), nullable=False),
        Column("source_file", Text, nullable=False),
        Column("source_row_number", Integer, nullable=False),
        Column("source_record_key", Text, nullable=False),
        Column("canonical_product_id", Text, nullable=False),
        Column("dataset_snapshot", String(10), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("raw_values", JSON, nullable=False),
        Column("quality_annotations", JSON, nullable=False),
        UniqueConstraint(
            "source_dataset",
            "source_record_key",
            "dataset_snapshot",
            name=f"uq_{name}_record_snapshot",
        ),
    )


source_domestic_bonds = _source_table("source_domestic_bonds")
source_domestic_etfs = _source_table("source_domestic_etfs")
source_foreign_etfs = _source_table("source_foreign_etfs")
source_public_funds = _source_table("source_public_funds")

canonical_products = Table(
    "canonical_products",
    metadata,
    Column("canonical_product_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("source_dataset", String(32), nullable=False),
    Column("source_record_key", Text, nullable=False),
    Column("source_file", Text, nullable=False),
    Column("source_row_number", Integer, nullable=False),
    Column("product_type", String(64), nullable=False),
    Column("product_name", Text, nullable=False),
    Column("short_name", Text),
    Column("normalized_product_name", Text, nullable=False),
    Column("normalized_short_name", Text),
    Column("ticker", Text),
    Column("isin", Text),
    Column("asset_manager", Text),
    Column("issuer", Text),
    Column("asset_type", String(64)),
    Column("region", String(64)),
    Column("risk_grade", Text),
    Column("currency", String(16)),
    Column("aum", Float),
    Column("nav", Float),
    Column("price", Float),
    Column("expense_ratio", Float),
    Column("base_index", Text),
    Column("observed_at", Text),
)
Index("ix_canonical_products_type", canonical_products.c.product_type)
Index("ix_canonical_products_region", canonical_products.c.region)
Index("ix_canonical_products_asset", canonical_products.c.asset_type)
Index("ix_canonical_products_name", canonical_products.c.product_name)
Index(
    "ix_canonical_products_normalized_name",
    canonical_products.c.normalized_product_name,
)
Index(
    "ix_canonical_products_normalized_short_name",
    canonical_products.c.normalized_short_name,
)

bond_attributes = Table(
    "bond_attributes",
    metadata,
    Column("canonical_product_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("issue_balance", Float),
    Column("issue_date", String(10)),
    Column("maturity_date", String(10)),
    Column("buy_yield", Float),
    Column("major_category", Text),
    Column("minor_category", Text),
    ForeignKeyConstraint(
        ["canonical_product_id", "dataset_snapshot"],
        [
            "canonical_products.canonical_product_id",
            "canonical_products.dataset_snapshot",
        ],
    ),
)

etf_attributes = Table(
    "etf_attributes",
    metadata,
    Column("canonical_product_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("strategy", Text),
    Column("replication_method", Text),
    Column("leverage_factor", Float),
    Column("distribution_cycle", Text),
    Column("raw_product_group", Text),
    ForeignKeyConstraint(
        ["canonical_product_id", "dataset_snapshot"],
        [
            "canonical_products.canonical_product_id",
            "canonical_products.dataset_snapshot",
        ],
    ),
)

funds = Table(
    "funds",
    metadata,
    Column("fund_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("source_fund_id", Text, nullable=False),
    Column("fund_name", Text, nullable=False),
    Column("representative_ksd_id", Text),
)

fund_classes = Table(
    "fund_classes",
    metadata,
    Column("canonical_product_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("fund_id", Text, nullable=False),
    Column("class_code", Text, nullable=False),
    Column("raw_asset_category", Text),
    Column("public_private", Text),
    ForeignKeyConstraint(
        ["canonical_product_id", "dataset_snapshot"],
        [
            "canonical_products.canonical_product_id",
            "canonical_products.dataset_snapshot",
        ],
    ),
    ForeignKeyConstraint(
        ["fund_id", "dataset_snapshot"],
        ["funds.fund_id", "funds.dataset_snapshot"],
    ),
)

product_identifiers = Table(
    "product_identifiers",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("canonical_product_id", Text, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("identifier_type", String(32), nullable=False),
    Column("identifier_value", Text, nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("source_dataset", String(32), nullable=False),
    UniqueConstraint(
        "canonical_product_id",
        "dataset_snapshot",
        "identifier_type",
        "identifier_value",
        "source_dataset",
        name="uq_product_identifier",
    ),
)
Index(
    "ix_product_identifiers_normalized",
    product_identifiers.c.normalized_value,
)

quarantine_records = Table(
    "quarantine_records",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_dataset", String(32), nullable=False),
    Column("source_file", Text, nullable=False),
    Column("source_row_number", Integer, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("ingestion_run_id", String(36)),
    Column("source_record_key", Text),
    Column("raw_product_name", Text),
    Column("failure_reason", Text),
    Column("raw_payload", JSON, nullable=False),
    UniqueConstraint(
        "source_dataset",
        "source_file",
        "source_row_number",
        "dataset_snapshot",
        name="uq_quarantine_source_row",
    ),
)

field_quality_profiles = Table(
    "field_quality_profiles",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("source_dataset", String(32), nullable=False),
    Column("product_type", String(64), nullable=False),
    Column("canonical_field", String(64), nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("total_count", Integer, nullable=False),
    Column("valid_count", Integer, nullable=False),
    Column("missing_count", Integer, nullable=False),
    Column("coverage_fraction", Float),
    Column("unique_count", Integer, nullable=False),
    Column("constant_value", Text),
    Column("is_constant", Boolean, nullable=False),
    UniqueConstraint(
        "source_dataset",
        "product_type",
        "canonical_field",
        "dataset_snapshot",
        name="uq_field_quality_scope",
    ),
)

# Ontology-guided, evidence-first ingestion tables. The existing canonical tables
# remain as the query-optimized compatibility projection.
ingestion_runs = Table(
    "ingestion_runs", metadata,
    Column("run_id", String(36), primary_key=True),
    Column("started_at", Text, nullable=False),
    Column("completed_at", Text),
    Column("status", String(24), nullable=False),
    Column("dry_run", Boolean, nullable=False),
    Column("options", JSON, nullable=False),
    Column("report", JSON),
)

source_datasets = Table(
    "source_datasets", metadata,
    Column("dataset_id", String(32), primary_key=True),
    Column("dataset_code", String(16), nullable=False),
    Column("source_table", String(64), nullable=False),
    Column("source_file", Text, nullable=False),
    Column("schema_file", Text, nullable=False),
    Column("snapshot_date", String(10), primary_key=True),
    Column("schema_column_count", Integer, nullable=False),
    Column("source_row_count", Integer, nullable=False),
    Column("data_sha256", String(64), nullable=False),
    Column("schema_sha256", String(64), nullable=False),
    Column("mapping_sha256", String(64), nullable=False),
    Column("transformer_version", String(32), nullable=False),
    Column("dataset_fingerprint", String(64), nullable=False),
    Column("ingestion_status", String(32), nullable=False),
    UniqueConstraint("dataset_code", "snapshot_date", name="uq_source_dataset_code_snapshot"),
)

source_records = Table(
    "source_records", metadata,
    Column("source_record_id", Text, primary_key=True),
    Column("dataset_id", String(32), nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("source_record_key", Text, nullable=False),
    Column("source_row_number", Integer, nullable=False),
    Column("canonical_product_id", Text, nullable=False),
    Column("raw_payload", JSON, nullable=False),
    Column("normalized_payload", JSON, nullable=False),
    Column("raw_payload_hash", String(64), nullable=False),
    Column("quality_annotations", JSON, nullable=False),
    UniqueConstraint("dataset_id", "dataset_snapshot", "source_record_key", name="uq_source_record_identity"),
)
Index("ix_source_records_product", source_records.c.canonical_product_id, source_records.c.dataset_snapshot)

metric_observations = Table(
    "metric_observations", metadata,
    Column("observation_id", Text, primary_key=True),
    Column("canonical_product_id", Text, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("source_record_id", Text, nullable=False),
    Column("source_dataset", String(32), nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("observation_type", String(64), nullable=False),
    Column("metric_type", String(128), nullable=False),
    Column("numeric_value", Numeric(38, 12)),
    Column("raw_value", Text, nullable=False),
    Column("unit", String(64)),
    Column("currency", String(16)),
    Column("observed_at", String(10)),
    Column("quality_status", String(32), nullable=False),
    Column("transformation_rule", Text, nullable=False),
    UniqueConstraint("source_record_id", "source_column", name="uq_observation_source_field"),
)
Index("ix_observations_product_metric_date", metric_observations.c.canonical_product_id, metric_observations.c.metric_type, metric_observations.c.observed_at)
Index("ix_observations_metric_value", metric_observations.c.metric_type, metric_observations.c.numeric_value)

product_relations = Table(
    "product_relations", metadata,
    Column("relation_id", Text, primary_key=True),
    Column("canonical_product_id", Text, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("source_record_id", Text, nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("relation_type", String(64), nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", Text, nullable=False),
    Column("target_label", Text, nullable=False),
    Column("identity_basis", String(64), nullable=False),
    UniqueConstraint("source_record_id", "source_column", "relation_type", "target_id", name="uq_product_relation_source"),
)
Index("ix_product_relations_subject", product_relations.c.canonical_product_id, product_relations.c.relation_type)
Index("ix_product_relations_target", product_relations.c.target_id, product_relations.c.relation_type)

source_field_assertions = Table(
    "source_field_assertions", metadata,
    Column("assertion_id", Text, primary_key=True),
    Column("source_record_id", Text, nullable=False),
    Column("canonical_product_id", Text, nullable=False),
    Column("source_dataset", String(32), nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("mapping_category", String(64), nullable=False),
    Column("target_class", String(96), nullable=False),
    Column("target_property", String(128), nullable=False),
    Column("raw_value", Text),
    Column("normalized_value", Text),
    Column("quality_status", String(32), nullable=False),
    Column("transformation_rule", Text, nullable=False),
    UniqueConstraint("source_record_id", "source_column", name="uq_source_field_assertion"),
)
Index("ix_assertions_product_column", source_field_assertions.c.canonical_product_id, source_field_assertions.c.source_column)

field_coverage_stats = Table(
    "field_coverage_stats", metadata,
    Column("source_dataset", String(32), primary_key=True),
    Column("dataset_snapshot", String(10), primary_key=True),
    Column("source_column", String(128), primary_key=True),
    Column("total_records", Integer, nullable=False),
    Column("present_records", Integer, nullable=False),
    Column("missing_records", Integer, nullable=False),
    Column("invalid_records", Integer, nullable=False),
    Column("sentinel_records", Integer, nullable=False),
    Column("coverage_fraction", Float, nullable=False),
)
Index("ix_coverage_dataset_column", field_coverage_stats.c.source_dataset, field_coverage_stats.c.source_column)

raw_code_values = Table(
    "raw_code_values", metadata,
    Column("raw_code_id", Text, primary_key=True),
    Column("source_record_id", Text, nullable=False),
    Column("canonical_product_id", Text, nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("code_value", Text, nullable=False),
    Column("code_scheme", Text, nullable=False),
    Column("quality_status", String(32), nullable=False),
    UniqueConstraint("source_record_id", "source_column", name="uq_raw_code_source"),
)

ontology_product_identifiers = Table(
    "ontology_product_identifiers", metadata,
    Column("identifier_id", Text, primary_key=True),
    Column("canonical_product_id", Text, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("source_record_id", Text, nullable=False),
    Column("source_dataset", String(32), nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("identifier_type", String(48), nullable=False),
    Column("identifier_value", Text, nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("namespace", String(64), nullable=False),
    Column("is_primary_in_source", Boolean, nullable=False),
    Column("validation_status", String(32), nullable=False),
    UniqueConstraint("canonical_product_id", "dataset_snapshot", "identifier_type", "normalized_value", "source_dataset", name="uq_ontology_product_identifier"),
)
Index("ix_ontology_identifiers_lookup", ontology_product_identifiers.c.identifier_type, ontology_product_identifiers.c.normalized_value)

identifier_conflicts = Table(
    "identifier_conflicts", metadata,
    Column("conflict_id", Text, primary_key=True),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("identifier_type", String(48), nullable=False),
    Column("namespace", String(64), nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("canonical_product_ids", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    UniqueConstraint("dataset_snapshot", "identifier_type", "namespace", "normalized_value", name="uq_identifier_conflict"),
)

semantic_source_documents = Table(
    "semantic_source_documents", metadata,
    Column("document_id", Text, primary_key=True),
    Column("canonical_product_id", Text, nullable=False),
    Column("source_record_id", Text, nullable=False),
    Column("source_dataset", String(32), nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("document_type", String(64), nullable=False),
    Column("raw_text", Text, nullable=False),
    Column("dataset_snapshot", String(10), nullable=False),
    Column("observed_at", String(10)),
    UniqueConstraint("source_record_id", "source_column", name="uq_semantic_document_source"),
)
