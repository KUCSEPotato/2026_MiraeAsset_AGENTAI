from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
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
