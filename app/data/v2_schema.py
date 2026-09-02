"""PostgreSQL-only Canonical Data Model v2 schema foundation.

M10.8-A deliberately keeps this metadata separate from the active v1 runtime
schema.  Alembic owns production creation of these objects; the metadata is
also useful for inspection and isolated schema tests.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.dialects.postgresql import JSONB


CANONICAL_V2_SCHEMA = "canonical_v2"
CANONICAL_V2_SCHEMA_VERSION = "m10.9-c2.6-canonical-v2"

_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(schema=CANONICAL_V2_SCHEMA, naming_convention=_NAMING_CONVENTION)


def _now() -> Column[DateTime]:
    return Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


schema_versions = Table(
    "schema_versions",
    metadata,
    Column("component", String(64), primary_key=True),
    Column("version", String(64), nullable=False),
    Column("installed_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

source_datasets = Table(
    "source_datasets",
    metadata,
    Column("dataset_id", String(64), primary_key=True),
    Column("dataset_code", String(32), nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("source_system", Text),
    Column("schema_contract_version", String(64), nullable=False),
    Column("is_authoritative", Boolean, nullable=False, server_default=text("false")),
    _now(),
)

dataset_snapshots = Table(
    "dataset_snapshots",
    metadata,
    Column("snapshot_id", String(96), primary_key=True),
    Column("dataset_id", String(64), ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_datasets.dataset_id"), nullable=False),
    Column("snapshot_date", Date, nullable=False),
    Column("generation", String(16), nullable=False, server_default=text("'UNSPECIFIED'")),
    Column("ontology_version", String(64), nullable=False, server_default=text("'UNSPECIFIED'")),
    Column("semantic_mapping_version", String(64), nullable=False, server_default=text("'UNSPECIFIED'")),
    Column("transformer_version", String(64), nullable=False, server_default=text("'UNSPECIFIED'")),
    Column("database_schema_version", String(64), nullable=False, server_default=text("'UNSPECIFIED'")),
    Column("data_sha256", String(64), nullable=False),
    Column("schema_sha256", String(64), nullable=False),
    Column("source_row_count", BigInteger, nullable=False),
    Column("accepted_row_count", BigInteger, nullable=False, server_default=text("0")),
    Column("quarantined_row_count", BigInteger, nullable=False, server_default=text("0")),
    Column("status", String(24), nullable=False, server_default=text("'STAGED'")),
    Column("reconciliation_status", String(24), nullable=False, server_default=text("'PENDING'")),
    Column("row_count_reconciled", Boolean, nullable=False, server_default=text("false")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _now(),
    UniqueConstraint("dataset_id", "snapshot_date", "data_sha256", name="dataset_snapshot_identity"),
    CheckConstraint("source_row_count >= 0 AND accepted_row_count >= 0 AND quarantined_row_count >= 0", name="nonnegative_counts"),
    CheckConstraint("status IN ('STAGED', 'VALIDATING', 'READY', 'FAILED', 'RETIRED')", name="status_allowed"),
    CheckConstraint("reconciliation_status IN ('PENDING', 'PASSED', 'FAILED')", name="reconciliation_status_allowed"),
    CheckConstraint("status <> 'READY' OR (reconciliation_status = 'PASSED' AND row_count_reconciled)", name="ready_requires_reconciliation"),
)
Index("ix_dataset_snapshots_dataset_date", dataset_snapshots.c.dataset_id, dataset_snapshots.c.snapshot_date)

ingestion_runs = Table(
    "ingestion_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.dataset_snapshots.snapshot_id"), nullable=False),
    Column("status", String(24), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("completed_at", DateTime(timezone=True)),
    Column("transformer_version", String(64), nullable=False),
    Column("options", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("report", JSONB),
    CheckConstraint("status IN ('STARTED', 'SUCCEEDED', 'FAILED', 'ROLLED_BACK')", name="status_allowed"),
)

source_records = Table(
    "source_records",
    metadata,
    Column("source_record_id", Text, primary_key=True),
    Column("snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.dataset_snapshots.snapshot_id"), nullable=False),
    Column("source_primary_key", Text, nullable=False),
    Column("source_row_number", BigInteger, nullable=False),
    Column("raw_payload", JSONB, nullable=False),
    Column("normalized_payload", JSONB),
    Column("payload_sha256", String(64), nullable=False),
    Column("quality_status", String(32), nullable=False, server_default=text("'UNVALIDATED'")),
    _now(),
    UniqueConstraint("snapshot_id", "source_primary_key", name="source_record_identity"),
    UniqueConstraint("snapshot_id", "source_row_number", name="source_record_row"),
    CheckConstraint("source_row_number > 0", name="positive_row_number"),
)

external_snapshot_manifests = Table(
    "external_snapshot_manifests",
    metadata,
    Column("external_snapshot_id", String(96), primary_key=True),
    Column("canonical_snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.dataset_snapshots.snapshot_id"), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("status", String(24), nullable=False),
    Column("data_cutoff_date", Date, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("manifest_json", JSONB, nullable=False),
    _now(),
    CheckConstraint("status IN ('READY', 'PARTIAL', 'FAILED')", name="status_allowed"),
    CheckConstraint("length(manifest_sha256) = 64", name="manifest_sha256_length"),
)

external_raw_artifacts = Table(
    "external_raw_artifacts",
    metadata,
    Column("artifact_id", Text, primary_key=True),
    Column("external_snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.external_snapshot_manifests.external_snapshot_id"), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("content_type", String(32), nullable=False),
    UniqueConstraint("external_snapshot_id", "sha256", "source_url", name="external_artifact_identity"),
    CheckConstraint("length(sha256) = 64", name="sha256_length"),
)

external_source_records = Table(
    "external_source_records",
    metadata,
    Column("external_source_record_id", Text, primary_key=True),
    Column("external_snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.external_snapshot_manifests.external_snapshot_id"), nullable=False),
    Column("artifact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.external_raw_artifacts.artifact_id"), nullable=False),
    Column("source_provider", Text, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("effective_date", Date, nullable=False),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("trust_tier", Integer, nullable=False),
    Column("quality_status", String(32), nullable=False),
    Column("raw_content_hash", String(64), nullable=False),
    CheckConstraint("trust_tier BETWEEN 1 AND 3", name="trust_tier_allowed"),
)

external_holding_records = Table(
    "external_holding_records",
    metadata,
    Column("holding_record_id", Text, primary_key=True),
    Column("external_source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.external_source_records.external_source_record_id"), nullable=False),
    Column("canonical_source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id"), nullable=False, unique=True),
    Column("product_source_id", Text, nullable=False),
    Column("constituent_source_id", Text),
    Column("effective_date", Date, nullable=False),
    Column("product_resolution_status", String(24), nullable=False),
    Column("security_resolution_status", String(24), nullable=False),
    Column("normalized_payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    CheckConstraint("product_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name="product_resolution_status_allowed"),
    CheckConstraint("security_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED', 'NON_SECURITY')", name="security_resolution_status_allowed"),
)

external_security_issuer_records = Table(
    "external_security_issuer_records",
    metadata,
    Column("issuer_record_id", Text, primary_key=True),
    Column(
        "external_source_record_id", Text,
        ForeignKey(f"{CANONICAL_V2_SCHEMA}.external_source_records.external_source_record_id"),
        nullable=False,
    ),
    Column(
        "canonical_source_record_id", Text,
        ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id"),
        nullable=False,
        unique=True,
    ),
    Column("security_ticker", Text, nullable=False),
    Column("security_source_id", Text, nullable=False),
    Column("issuer_source_id", Text, nullable=False),
    Column("effective_date", Date, nullable=False),
    Column("security_identity_status", String(24), nullable=False),
    Column("issuer_identity_status", String(24), nullable=False),
    Column("relation_validation_status", String(24), nullable=False),
    Column("normalized_payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    CheckConstraint(
        "security_identity_status IN ('RESOLVED', 'AMBIGUOUS', 'CONFLICT', 'UNRESOLVED')",
        name="security_identity_status_allowed",
    ),
    CheckConstraint(
        "issuer_identity_status IN ('RESOLVED', 'AMBIGUOUS', 'CONFLICT', 'UNRESOLVED')",
        name="issuer_identity_status_allowed",
    ),
    CheckConstraint(
        "relation_validation_status IN ('RESOLVED', 'AMBIGUOUS', 'CONFLICT', 'UNRESOLVED')",
        name="relation_validation_status_allowed",
    ),
    CheckConstraint("length(payload_sha256) = 64", name="payload_sha256_length"),
)

external_metric_records = Table(
    "external_metric_records",
    metadata,
    Column("metric_observation_id", Text, primary_key=True),
    Column(
        "external_source_record_id", Text,
        ForeignKey(
            f"{CANONICAL_V2_SCHEMA}.external_source_records.external_source_record_id"
        ), nullable=False,
    ),
    Column(
        "canonical_source_record_id", Text,
        ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id"),
        nullable=False, unique=True,
    ),
    Column("product_source_id", Text, nullable=False),
    Column("metric_code", String(64), nullable=False),
    Column("observation_end_date", Date, nullable=False),
    Column("product_resolution_status", String(24), nullable=False),
    Column("normalized_payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    CheckConstraint(
        "product_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')",
        name="product_resolution_status_allowed",
    ),
    CheckConstraint("length(payload_sha256) = 64", name="payload_sha256_length"),
)

quarantine_records = Table(
    "quarantine_records",
    metadata,
    Column("quarantine_id", BigInteger, primary_key=True, autoincrement=True),
    Column("snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.dataset_snapshots.snapshot_id"), nullable=False),
    Column("ingestion_run_id", String(64), ForeignKey(f"{CANONICAL_V2_SCHEMA}.ingestion_runs.run_id")),
    Column("source_row_number", BigInteger, nullable=False),
    Column("source_primary_key", Text),
    Column("reason_code", String(64), nullable=False),
    Column("failure_reason", Text, nullable=False),
    Column("raw_payload", JSONB, nullable=False),
    Column("status", String(24), nullable=False, server_default=text("'OPEN'")),
    _now(),
    UniqueConstraint("snapshot_id", "source_row_number", name="quarantine_source_row"),
    CheckConstraint("status IN ('OPEN', 'REVIEWED', 'RESOLVED', 'REJECTED')", name="status_allowed"),
)

canonical_entities = Table(
    "canonical_entities",
    metadata,
    Column("entity_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False),
    Column("preferred_name", Text),
    Column("normalized_preferred_name", Text),
    Column("name_status", String(48), nullable=False),
    Column("identity_status", String(24), nullable=False, server_default=text("'PROVISIONAL'")),
    Column("query_eligible", Boolean, nullable=False, server_default=text("false")),
    _now(),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("entity_id", "entity_kind", name="entity_kind_identity"),
    CheckConstraint("entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'ORGANIZATION', 'INDEX', 'CURRENCY', 'COUNTRY', 'SECURITY')", name="kind_allowed"),
    CheckConstraint("name_status IN ('AUTHORITATIVE', 'SOURCE_ONLY', 'NO_AUTHORITATIVE_FAMILY_NAME', 'UNKNOWN')", name="name_status_allowed"),
    CheckConstraint("name_status <> 'NO_AUTHORITATIVE_FAMILY_NAME' OR preferred_name IS NULL", name="missing_family_name_is_null"),
    CheckConstraint("identity_status IN ('PROVISIONAL', 'VALIDATED', 'AMBIGUOUS', 'CONFLICT', 'RETIRED')", name="identity_status_allowed"),
)
Index("ix_canonical_entities_kind", canonical_entities.c.entity_kind)
Index("ix_canonical_entities_normalized_name", canonical_entities.c.normalized_preferred_name)

product_types = Table(
    "product_types",
    metadata,
    Column("product_type_code", String(16), primary_key=True),
    Column("label", Text, nullable=False),
    Column("ontology_iri", Text, nullable=False, unique=True),
    CheckConstraint("product_type_code IN ('ETF', 'ETN', 'BOND', 'FUND')", name="allowed_product_type"),
)

financial_products = Table(
    "financial_products",
    metadata,
    Column("product_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'FINANCIAL_PRODUCT'")),
    Column("product_type_code", String(16), ForeignKey(f"{CANONICAL_V2_SCHEMA}.product_types.product_type_code"), nullable=False),
    ForeignKeyConstraint(
        ["product_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_financial_product_entity_kind",
    ),
    UniqueConstraint("product_id", "product_type_code", name="product_type_identity"),
    CheckConstraint("entity_kind = 'FINANCIAL_PRODUCT'", name="financial_product_kind"),
    CheckConstraint("product_type_code IN ('ETF', 'ETN', 'BOND', 'FUND')", name="allowed_product_type"),
)
Index("ix_financial_products_type", financial_products.c.product_type_code)

bonds = Table(
    "bonds",
    metadata,
    Column("bond_id", Text, primary_key=True),
    Column("product_type_code", String(16), nullable=False, server_default=text("'BOND'")),
    ForeignKeyConstraint(
        ["bond_id", "product_type_code"],
        [f"{CANONICAL_V2_SCHEMA}.financial_products.product_id", f"{CANONICAL_V2_SCHEMA}.financial_products.product_type_code"],
        name="fk_bond_product_type",
    ),
    Column("issue_date", Date),
    Column("maturity_date", Date),
    CheckConstraint("product_type_code = 'BOND'", name="bond_product_type"),
)

exchange_traded_products = Table(
    "exchange_traded_products",
    metadata,
    Column("etp_id", Text, primary_key=True),
    Column("product_type_code", String(16), nullable=False),
    ForeignKeyConstraint(
        ["etp_id", "product_type_code"],
        [f"{CANONICAL_V2_SCHEMA}.financial_products.product_id", f"{CANONICAL_V2_SCHEMA}.financial_products.product_type_code"],
        name="fk_etp_product_type",
    ),
    Column("listing_date", Date),
    Column("delisting_date", Date),
    CheckConstraint("product_type_code IN ('ETF', 'ETN')", name="etp_product_type"),
)

funds = Table(
    "funds",
    metadata,
    Column("fund_id", Text, primary_key=True),
    Column("product_type_code", String(16), nullable=False, server_default=text("'FUND'")),
    ForeignKeyConstraint(
        ["fund_id", "product_type_code"],
        [f"{CANONICAL_V2_SCHEMA}.financial_products.product_id", f"{CANONICAL_V2_SCHEMA}.financial_products.product_type_code"],
        name="fk_fund_product_type",
    ),
    Column("inception_date", Date),
    CheckConstraint("product_type_code = 'FUND'", name="fund_product_type"),
)

fund_share_classes = Table(
    "fund_share_classes",
    metadata,
    Column("fund_share_class_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'FUND_SHARE_CLASS'")),
    Column("parent_fund_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.funds.fund_id"), nullable=False),
    Column("source_class_key", Text, nullable=False),
    ForeignKeyConstraint(
        ["fund_share_class_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_fund_share_class_entity_kind",
    ),
    UniqueConstraint("parent_fund_id", "source_class_key", name="fund_share_class_parent_key"),
    CheckConstraint("entity_kind = 'FUND_SHARE_CLASS'", name="fund_share_class_kind"),
)
Index("ix_fund_share_classes_parent", fund_share_classes.c.parent_fund_id)

sale_lots = Table(
    "sale_lots",
    metadata,
    Column("sale_lot_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'SALE_LOT'")),
    Column("bond_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.bonds.bond_id"), nullable=False),
    Column("trading_market_raw", Text, nullable=False),
    Column("information_date", Date, nullable=False),
    Column("lot_sequence", Integer, nullable=False, server_default=text("1")),
    ForeignKeyConstraint(
        ["sale_lot_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_sale_lot_entity_kind",
    ),
    UniqueConstraint("bond_id", "trading_market_raw", "information_date", "lot_sequence", name="sale_lot_natural_key"),
    CheckConstraint("entity_kind = 'SALE_LOT'", name="sale_lot_kind"),
    CheckConstraint("lot_sequence > 0", name="positive_lot_sequence"),
)
Index("ix_sale_lots_bond", sale_lots.c.bond_id)

organizations = Table(
    "organizations",
    metadata,
    Column("organization_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'ORGANIZATION'")),
    Column("organization_type", String(32), nullable=False),
    ForeignKeyConstraint(
        ["organization_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_organization_entity_kind",
    ),
    CheckConstraint("entity_kind = 'ORGANIZATION'", name="organization_kind"),
    CheckConstraint("organization_type IN ('ASSET_MANAGER', 'ISSUER', 'TRUSTEE', 'DISTRIBUTOR', 'OTHER')", name="organization_type_allowed"),
)

securities = Table(
    "securities",
    metadata,
    Column("security_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'SECURITY'")),
    Column("security_type", String(32), nullable=False),
    Column("ticker", Text),
    Column("isin", Text),
    Column("exchange", Text),
    Column("issuer_resolution_status", String(24), nullable=False, server_default=text("'UNRESOLVED'")),
    ForeignKeyConstraint(
        ["security_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_security_entity_kind",
    ),
    CheckConstraint("entity_kind = 'SECURITY'", name="security_kind"),
    CheckConstraint("security_type IN ('EQUITY')", name="security_type_allowed"),
    CheckConstraint("issuer_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name="issuer_resolution_status_allowed"),
)
Index("ix_securities_ticker_exchange", securities.c.ticker, securities.c.exchange)
Index("ix_securities_isin", securities.c.isin)

indices = Table(
    "indices",
    metadata,
    Column("index_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False, server_default=text("'INDEX'")),
    Column("resolution_status", String(24), nullable=False),
    ForeignKeyConstraint(
        ["index_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_index_entity_kind",
    ),
    CheckConstraint("entity_kind = 'INDEX'", name="index_kind"),
    CheckConstraint("resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name="resolution_status_allowed"),
)

entity_aliases = Table(
    "entity_aliases",
    metadata,
    Column("alias_id", BigInteger, primary_key=True, autoincrement=True),
    Column("entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("alias", Text, nullable=False),
    Column("normalized_alias", Text, nullable=False),
    Column("alias_type", String(32), nullable=False),
    Column("language", String(16)),
    Column("source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id")),
    Column("is_preferred", Boolean, nullable=False, server_default=text("false")),
    UniqueConstraint("entity_id", "normalized_alias", "alias_type", name="entity_alias_identity"),
)
Index("ix_entity_aliases_lookup", entity_aliases.c.normalized_alias)

identifier_schemes = Table(
    "identifier_schemes",
    metadata,
    Column("scheme_code", String(48), primary_key=True),
    Column("label", Text, nullable=False),
    Column("default_namespace", Text),
    Column("validation_pattern", Text),
    Column("is_globally_unique", Boolean, nullable=False, server_default=text("false")),
)

entity_identifiers = Table(
    "entity_identifiers",
    metadata,
    Column("identifier_id", BigInteger, primary_key=True, autoincrement=True),
    Column("entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("scheme_code", String(48), ForeignKey(f"{CANONICAL_V2_SCHEMA}.identifier_schemes.scheme_code"), nullable=False),
    Column("namespace", Text, nullable=False),
    Column("raw_value", Text, nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("validation_status", String(24), nullable=False),
    Column("resolution_status", String(24), nullable=False),
    Column("conflict_status", String(24), nullable=False, server_default=text("'NONE'")),
    Column("is_primary", Boolean, nullable=False, server_default=text("false")),
    Column("source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id")),
    _now(),
    UniqueConstraint("entity_id", "scheme_code", "namespace", "normalized_value", name="entity_identifier_identity"),
    CheckConstraint("validation_status IN ('VALIDATED', 'UNVALIDATED', 'INVALID')", name="validation_status_allowed"),
    CheckConstraint("resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name="resolution_status_allowed"),
    CheckConstraint("conflict_status IN ('NONE', 'OPEN', 'RESOLVED')", name="conflict_status_allowed"),
)
Index("ix_entity_identifiers_lookup", entity_identifiers.c.scheme_code, entity_identifiers.c.namespace, entity_identifiers.c.normalized_value)
Index(
    "uq_entity_identifiers_validated_global",
    entity_identifiers.c.scheme_code,
    entity_identifiers.c.namespace,
    entity_identifiers.c.normalized_value,
    unique=True,
    postgresql_where=text("validation_status = 'VALIDATED' AND conflict_status = 'NONE'"),
)

identifier_collision_cases = Table(
    "identifier_collision_cases",
    metadata,
    Column("collision_case_id", Text, primary_key=True),
    Column("scheme_code", String(48), ForeignKey(f"{CANONICAL_V2_SCHEMA}.identifier_schemes.scheme_code"), nullable=False),
    Column("namespace", Text, nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("candidate_entity_ids", JSONB, nullable=False),
    Column("status", String(24), nullable=False),
    Column("resolution_notes", Text),
    _now(),
    UniqueConstraint("scheme_code", "namespace", "normalized_value", "status", name="identifier_collision_state"),
    CheckConstraint("status IN ('OPEN', 'RESOLVED', 'REJECTED')", name="status_allowed"),
)

ontology_concepts = Table(
    "ontology_concepts",
    metadata,
    Column("concept_iri", Text, primary_key=True),
    Column("concept_category", String(64), nullable=False),
    Column("canonical_name", Text, nullable=False),
    Column("ontology_version", String(64), nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("true")),
)
Index("ix_ontology_concepts_category", ontology_concepts.c.concept_category)

source_field_assertions = Table(
    "source_field_assertions",
    metadata,
    Column("assertion_id", Text, primary_key=True),
    Column("source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id"), nullable=False),
    Column("source_column", String(128), nullable=False),
    Column("raw_value", Text),
    Column("normalized_value", Text),
    Column("mapping_category", String(48), nullable=False),
    Column("target_semantic_key", Text),
    Column("quality_status", String(32), nullable=False),
    Column("transformation_rule", Text, nullable=False),
    UniqueConstraint("source_record_id", "source_column", "mapping_category", name="source_field_assertion_identity"),
)
Index("ix_source_field_assertions_record", source_field_assertions.c.source_record_id)

source_record_entities = Table(
    "source_record_entities",
    metadata,
    Column("source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id"), primary_key=True),
    Column("entity_id", Text, primary_key=True),
    Column("entity_kind", String(32), nullable=False),
    Column("provenance_role", String(32), nullable=False, server_default=text("'DESCRIBES'")),
    ForeignKeyConstraint(
        ["entity_id", "entity_kind"],
        [f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id", f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_kind"],
        name="fk_source_record_entity_kind",
    ),
    CheckConstraint("entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'SECURITY')", name="evidence_bearing_kind"),
    CheckConstraint("provenance_role IN ('DESCRIBES', 'SUPPORTS')", name="provenance_role_allowed"),
)
Index("ix_source_record_entities_entity", source_record_entities.c.entity_id)
Index(
    "uq_source_record_entities_one_describes",
    source_record_entities.c.source_record_id,
    unique=True,
    postgresql_where=text("provenance_role = 'DESCRIBES'"),
)

canonical_facts = Table(
    "canonical_facts",
    metadata,
    Column("fact_id", Text, primary_key=True),
    Column("subject_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("snapshot_id", String(96), ForeignKey(f"{CANONICAL_V2_SCHEMA}.dataset_snapshots.snapshot_id"), nullable=False),
    Column("fact_kind", String(48), nullable=False),
    Column("semantic_key", Text, nullable=False),
    Column("resolution_status", String(24), nullable=False),
    Column("valid_from", Date),
    Column("valid_to", Date),
    _now(),
    UniqueConstraint("subject_entity_id", "snapshot_id", "fact_kind", "semantic_key", name="canonical_fact_identity"),
    CheckConstraint("fact_kind IN ('SCALAR', 'ENTITY_RELATION', 'ORGANIZATION_RELATION', 'INDEX_RELATION', 'CLASSIFICATION', 'METRIC')", name="fact_kind_allowed"),
    CheckConstraint("resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED', 'CONFLICT')", name="resolution_status_allowed"),
)

canonical_scalar_facts = Table(
    "canonical_scalar_facts",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("value_type", String(16), nullable=False),
    Column("text_value", Text),
    Column("numeric_value", Numeric(38, 15)),
    Column("date_value", Date),
    Column("boolean_value", Boolean),
    CheckConstraint("value_type IN ('TEXT', 'NUMERIC', 'DATE', 'BOOLEAN')", name="value_type_allowed"),
    CheckConstraint(
        "(value_type = 'TEXT' AND text_value IS NOT NULL AND numeric_value IS NULL AND date_value IS NULL AND boolean_value IS NULL) OR "
        "(value_type = 'NUMERIC' AND text_value IS NULL AND numeric_value IS NOT NULL AND date_value IS NULL AND boolean_value IS NULL) OR "
        "(value_type = 'DATE' AND text_value IS NULL AND numeric_value IS NULL AND date_value IS NOT NULL AND boolean_value IS NULL) OR "
        "(value_type = 'BOOLEAN' AND text_value IS NULL AND numeric_value IS NULL AND date_value IS NULL AND boolean_value IS NOT NULL)",
        name="exactly_one_typed_value",
    ),
)

entity_relations = Table(
    "entity_relations",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("subject_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("relation_type", String(40), nullable=False),
    Column("object_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    # Snapshot/effective-time identity lives on canonical_facts.  A global
    # subject/relation/object uniqueness rule would incorrectly collapse the
    # same temporal relation across snapshots.
    CheckConstraint(
        "relation_type IN ('HAS_SHARE_CLASS', 'HAS_SALE_LOT', 'MANAGED_BY', 'ISSUED_BY', 'HAS_TRUSTEE', 'HAS_UNDERLYING_INDEX', 'TRACKS_INDEX', 'HAS_BENCHMARK', 'DENOMINATED_IN', 'TRADED_IN_CURRENCY', 'LISTED_IN_COUNTRY', 'HAS_INSTRUMENT_COUNTRY', 'HOLDS', 'SECURITY_ISSUED_BY')",
        name="relation_type_allowed",
    ),
)
Index("ix_entity_relations_target", entity_relations.c.object_entity_id, entity_relations.c.relation_type)

holding_fact_details = Table(
    "holding_fact_details",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.entity_relations.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("effective_date", Date, nullable=False),
    Column("weight_normalized", Numeric(38, 15)),
    Column("weight_unit", String(64)),
    Column("weight_scale", String(32)),
    Column("source_provider", Text, nullable=False),
    Column("external_holding_record_id", Text, nullable=False),
    CheckConstraint("weight_normalized IS NULL OR (weight_normalized >= 0 AND weight_normalized <= 1)", name="weight_proportion"),
    CheckConstraint("(weight_normalized IS NULL AND weight_unit IS NULL AND weight_scale IS NULL) OR (weight_normalized IS NOT NULL AND weight_unit IS NOT NULL AND weight_scale IS NOT NULL)", name="weight_semantic_tuple"),
)

fact_evidence_links = Table(
    "fact_evidence_links",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("assertion_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_field_assertions.assertion_id"), primary_key=True),
    Column("evidence_role", String(24), nullable=False, server_default=text("'SUPPORTS'")),
    CheckConstraint("evidence_role IN ('SUPPORTS', 'CONTRADICTS', 'DERIVES')", name="evidence_role_allowed"),
)

organization_relations = Table(
    "organization_relations",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("subject_product_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.financial_products.product_id"), nullable=False),
    Column("relation_type", String(32), nullable=False),
    Column("organization_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.organizations.organization_id"), nullable=False),
    UniqueConstraint("subject_product_id", "relation_type", "organization_id", name="organization_relation_identity"),
    CheckConstraint("relation_type IN ('MANAGED_BY', 'ISSUED_BY', 'HAS_TRUSTEE')", name="relation_type_allowed"),
)
Index("ix_organization_relations_target", organization_relations.c.organization_id, organization_relations.c.relation_type)

index_relations = Table(
    "index_relations",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("subject_product_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.financial_products.product_id"), nullable=False),
    Column("relation_type", String(32), nullable=False),
    Column("index_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.indices.index_id"), nullable=False),
    UniqueConstraint("subject_product_id", "relation_type", "index_id", name="index_relation_identity"),
    CheckConstraint("relation_type IN ('HAS_UNDERLYING_INDEX', 'TRACKS_INDEX', 'HAS_BENCHMARK')", name="relation_type_allowed"),
)
Index("ix_index_relations_target", index_relations.c.index_id, index_relations.c.relation_type)

entity_classifications = Table(
    "entity_classifications",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("concept_iri", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.ontology_concepts.concept_iri"), nullable=False),
    Column("classification_type", String(64), nullable=False),
    UniqueConstraint("entity_id", "classification_type", "concept_iri", name="entity_classification_identity"),
)
Index("ix_entity_classifications_concept", entity_classifications.c.concept_iri, entity_classifications.c.classification_type)

source_classification_values = Table(
    "source_classification_values",
    metadata,
    Column("source_classification_value_id", BigInteger, primary_key=True, autoincrement=True),
    Column("assertion_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_field_assertions.assertion_id"), nullable=False),
    Column("raw_value", Text, nullable=False),
    Column("normalized_value", Text),
    Column("candidate_concept_iri", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.ontology_concepts.concept_iri")),
    Column("resolution_status", String(24), nullable=False),
    Column("resolution_rule", Text),
    UniqueConstraint("assertion_id", "raw_value", name="source_classification_value_identity"),
    CheckConstraint("resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name="resolution_status_allowed"),
)

metric_definitions = Table(
    "metric_definitions",
    metadata,
    Column("metric_code", String(64), primary_key=True),
    Column("canonical_field", Text, nullable=False, unique=True),
    Column("label", Text, nullable=False),
    Column("value_type", String(16), nullable=False, server_default=text("'NUMERIC'")),
    Column("expected_unit", String(32)),
    Column("expected_scale_basis", String(32)),
    Column("cross_source_comparable", Boolean, nullable=False, server_default=text("false")),
    Column("filter_enabled", Boolean, nullable=False, server_default=text("false")),
    Column("sort_enabled", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("value_type IN ('NUMERIC', 'DATE')", name="value_type_allowed"),
)

metric_observations = Table(
    "metric_observations",
    metadata,
    Column("fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id", ondelete="CASCADE"), primary_key=True),
    Column("metric_code", String(64), ForeignKey(f"{CANONICAL_V2_SCHEMA}.metric_definitions.metric_code"), nullable=False),
    Column("subject_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("raw_value", Text),
    Column("numeric_value", Numeric(38, 15)),
    Column("date_value", Date),
    Column("unit", String(32)),
    Column("scale_basis", String(32)),
    Column("currency", String(16)),
    Column("observed_on", Date),
    Column("quality_status", String(32), nullable=False),
    Column("comparability_status", String(32), nullable=False, server_default=text("'UNKNOWN'")),
    CheckConstraint("quality_status IN ('VALID', 'SOURCE_ZERO', 'MISSING', 'NOT_APPLICABLE', 'INVALID', 'UNKNOWN')", name="quality_status_allowed"),
    CheckConstraint("comparability_status IN ('COMPARABLE', 'NOT_COMPARABLE', 'UNKNOWN')", name="comparability_status_allowed"),
    CheckConstraint("quality_status NOT IN ('MISSING', 'NOT_APPLICABLE') OR (numeric_value IS NULL AND date_value IS NULL)", name="missing_has_no_typed_value"),
    CheckConstraint("quality_status <> 'SOURCE_ZERO' OR (numeric_value = 0 AND date_value IS NULL)", name="source_zero_is_numeric_zero"),
    CheckConstraint("quality_status <> 'VALID' OR ((numeric_value IS NOT NULL) <> (date_value IS NOT NULL))", name="valid_has_one_typed_value"),
    CheckConstraint("comparability_status <> 'COMPARABLE' OR (quality_status IN ('VALID', 'SOURCE_ZERO') AND unit IS NOT NULL AND scale_basis IS NOT NULL)", name="comparable_requires_contract"),
)
Index("ix_metric_observations_subject_metric_date", metric_observations.c.subject_entity_id, metric_observations.c.metric_code, metric_observations.c.observed_on)
Index("ix_metric_observations_metric_numeric", metric_observations.c.metric_code, metric_observations.c.numeric_value)

identity_resolution_cases = Table(
    "identity_resolution_cases",
    metadata,
    Column("resolution_case_id", Text, primary_key=True),
    Column("source_record_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.source_records.source_record_id")),
    Column("raw_identity", JSONB, nullable=False),
    Column("candidate_entity_ids", JSONB, nullable=False),
    Column("resolution_status", String(24), nullable=False),
    Column("reason_code", String(64)),
    Column("resolved_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id")),
    Column("resolution_rule", Text),
    Column("review_notes", Text),
    _now(),
    CheckConstraint("resolution_status IN ('OPEN', 'RESOLVED', 'AMBIGUOUS', 'UNRESOLVED', 'REJECTED')", name="resolution_status_allowed"),
    CheckConstraint("resolution_status <> 'RESOLVED' OR resolved_entity_id IS NOT NULL", name="resolved_has_entity"),
)

fact_conflict_cases = Table(
    "fact_conflict_cases",
    metadata,
    Column("conflict_case_id", Text, primary_key=True),
    Column("subject_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id"), nullable=False),
    Column("semantic_key", Text, nullable=False),
    Column("candidate_fact_ids", JSONB, nullable=False),
    Column("status", String(24), nullable=False),
    Column("winning_fact_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_facts.fact_id")),
    Column("resolution_notes", Text),
    _now(),
    CheckConstraint("status IN ('OPEN', 'RESOLVED', 'UNRESOLVED', 'REJECTED')", name="status_allowed"),
    CheckConstraint("status <> 'RESOLVED' OR winning_fact_id IS NOT NULL", name="resolved_has_winner"),
)

entity_id_crosswalk = Table(
    "entity_id_crosswalk",
    metadata,
    Column("crosswalk_id", BigInteger, primary_key=True, autoincrement=True),
    Column("v1_entity_id", Text, nullable=False),
    Column("v1_entity_type", String(64), nullable=False),
    Column("v2_entity_id", Text, ForeignKey(f"{CANONICAL_V2_SCHEMA}.canonical_entities.entity_id")),
    Column("mapping_status", String(24), nullable=False),
    Column("mapping_basis", Text, nullable=False),
    Column("review_notes", Text),
    _now(),
    UniqueConstraint("v1_entity_id", "v1_entity_type", "v2_entity_id", name="entity_id_crosswalk_identity"),
    CheckConstraint("mapping_status IN ('EXACT', 'SPLIT', 'MERGED', 'AMBIGUOUS', 'REJECTED', 'RETIRED')", name="mapping_status_allowed"),
    CheckConstraint("(mapping_status = 'RETIRED' AND v2_entity_id IS NULL) OR (mapping_status <> 'RETIRED' AND v2_entity_id IS NOT NULL)", name="retired_has_no_v2_target"),
)


SEED_PRODUCT_TYPES = (
    {"product_type_code": "ETF", "label": "Exchange Traded Fund", "ontology_iri": "https://miraeasset.com/ontology/financial-product#ETF"},
    {"product_type_code": "ETN", "label": "Exchange Traded Note", "ontology_iri": "https://miraeasset.com/ontology/financial-product#ETN"},
    {"product_type_code": "BOND", "label": "Bond", "ontology_iri": "https://miraeasset.com/ontology/financial-product#Bond"},
    {"product_type_code": "FUND", "label": "Fund", "ontology_iri": "https://miraeasset.com/ontology/financial-product#Fund"},
)

SEED_IDENTIFIER_SCHEMES = tuple(
    {
        "scheme_code": code,
        "label": label,
        "default_namespace": namespace,
        "is_globally_unique": globally_unique,
    }
    for code, label, namespace, globally_unique in (
        ("SOURCE_ID", "Source identifier", "source", False),
        ("ISIN", "International Securities Identification Number", "iso-6166", True),
        ("TICKER", "Exchange ticker", "exchange", False),
        ("RIC", "Reuters Instrument Code", "refinitiv", True),
        ("LIPPER_ID", "Lipper identifier", "lipper", True),
        ("MA_ID", "Mirae Asset identifier", "miraeasset", True),
        ("KSD_ID", "Korea Securities Depository identifier", "ksd", True),
        ("REPRESENTATIVE_KSD_ID", "Representative KSD fund identifier", "ksd", True),
        ("FSS_ID", "Financial Supervisory Service identifier", "fss", True),
    )
)

SEED_METRIC_DEFINITIONS = (
    {
        "metric_code": "AUM",
        "canonical_field": "product.aum",
        "label": "Assets under management",
        "expected_unit": None,
        "expected_scale_basis": None,
        "cross_source_comparable": False,
        "filter_enabled": False,
        "sort_enabled": False,
    },
    {
        "metric_code": "EXPENSE_RATIO",
        "canonical_field": "product.expense_ratio",
        "label": "Expense ratio",
        "expected_unit": None,
        "expected_scale_basis": None,
        "cross_source_comparable": False,
        "filter_enabled": False,
        "sort_enabled": False,
    },
)


def get_schema_version(connection: Connection) -> str | None:
    """Report the installed canonical v2 version without changing runtime state."""
    return connection.scalar(
        select(schema_versions.c.version).where(
            schema_versions.c.component == "canonical_data_model"
        )
    )
