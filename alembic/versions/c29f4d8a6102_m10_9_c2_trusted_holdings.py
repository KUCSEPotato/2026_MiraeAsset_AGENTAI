"""M10.9-C2 trusted holdings canonical integration.

Revision ID: c29f4d8a6102
Revises: 8c21f3a1b7d4
Create Date: 2026-08-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c29f4d8a6102"
down_revision: Union[str, Sequence[str], None] = "8c21f3a1b7d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "canonical_v2"
    op.drop_constraint(op.f("ck_canonical_entities_kind_allowed"), "canonical_entities", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_canonical_entities_kind_allowed"), "canonical_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'ORGANIZATION', 'INDEX', 'CURRENCY', 'COUNTRY', 'SECURITY')", schema=schema)
    op.drop_constraint(op.f("ck_source_record_entities_evidence_bearing_kind"), "source_record_entities", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_source_record_entities_evidence_bearing_kind"), "source_record_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'SECURITY')", schema=schema)
    op.drop_constraint(op.f("ck_entity_relations_relation_type_allowed"), "entity_relations", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_entity_relations_relation_type_allowed"), "entity_relations",
        "relation_type IN ('HAS_SHARE_CLASS', 'HAS_SALE_LOT', 'MANAGED_BY', 'ISSUED_BY', 'HAS_TRUSTEE', 'HAS_UNDERLYING_INDEX', 'TRACKS_INDEX', 'HAS_BENCHMARK', 'DENOMINATED_IN', 'TRADED_IN_CURRENCY', 'LISTED_IN_COUNTRY', 'HAS_INSTRUMENT_COUNTRY', 'HOLDS', 'SECURITY_ISSUED_BY')", schema=schema)
    op.drop_constraint("entity_relation_identity", "entity_relations", schema=schema, type_="unique")

    op.create_table("securities",
        sa.Column("security_id", sa.Text(), nullable=False),
        sa.Column("entity_kind", sa.String(32), server_default=sa.text("'SECURITY'"), nullable=False),
        sa.Column("security_type", sa.String(32), nullable=False),
        sa.Column("ticker", sa.Text()), sa.Column("isin", sa.Text()), sa.Column("exchange", sa.Text()),
        sa.Column("issuer_resolution_status", sa.String(24), server_default=sa.text("'UNRESOLVED'"), nullable=False),
        sa.CheckConstraint("entity_kind = 'SECURITY'", name=op.f("ck_securities_security_kind")),
        sa.CheckConstraint("security_type IN ('EQUITY')", name=op.f("ck_securities_security_type_allowed")),
        sa.CheckConstraint("issuer_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name=op.f("ck_securities_issuer_resolution_status_allowed")),
        sa.ForeignKeyConstraint(["security_id", "entity_kind"], ["canonical_v2.canonical_entities.entity_id", "canonical_v2.canonical_entities.entity_kind"], name="fk_security_entity_kind"),
        sa.PrimaryKeyConstraint("security_id", name=op.f("pk_securities")), schema=schema)
    op.create_index("ix_securities_ticker_exchange", "securities", ["ticker", "exchange"], schema=schema)
    op.create_index("ix_securities_isin", "securities", ["isin"], schema=schema)

    op.create_table("external_snapshot_manifests",
        sa.Column("external_snapshot_id", sa.String(96), nullable=False),
        sa.Column("canonical_snapshot_id", sa.String(96), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("data_cutoff_date", sa.Date(), nullable=False), sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("status IN ('READY', 'PARTIAL', 'FAILED')", name=op.f("ck_external_snapshot_manifests_status_allowed")),
        sa.CheckConstraint("length(manifest_sha256) = 64", name=op.f("ck_external_snapshot_manifests_manifest_sha256_length")),
        sa.ForeignKeyConstraint(["canonical_snapshot_id"], ["canonical_v2.dataset_snapshots.snapshot_id"], name=op.f("fk_external_snapshot_manifests_canonical_snapshot_id_dataset_snapshots")),
        sa.PrimaryKeyConstraint("external_snapshot_id", name=op.f("pk_external_snapshot_manifests")), schema=schema)
    op.create_table("external_raw_artifacts",
        sa.Column("artifact_id", sa.Text(), nullable=False), sa.Column("external_snapshot_id", sa.String(96), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False), sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False), sa.Column("content_type", sa.String(32), nullable=False),
        sa.CheckConstraint("length(sha256) = 64", name=op.f("ck_external_raw_artifacts_sha256_length")),
        sa.ForeignKeyConstraint(["external_snapshot_id"], ["canonical_v2.external_snapshot_manifests.external_snapshot_id"], name=op.f("fk_external_raw_artifacts_external_snapshot_id_external_snapshot_manifests")),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_external_raw_artifacts")),
        sa.UniqueConstraint("external_snapshot_id", "sha256", "source_url", name="external_artifact_identity"), schema=schema)
    op.create_table("external_source_records",
        sa.Column("external_source_record_id", sa.Text(), nullable=False), sa.Column("external_snapshot_id", sa.String(96), nullable=False),
        sa.Column("artifact_id", sa.Text(), nullable=False), sa.Column("source_provider", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False), sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False), sa.Column("trust_tier", sa.Integer(), nullable=False),
        sa.Column("quality_status", sa.String(32), nullable=False), sa.Column("raw_content_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("trust_tier BETWEEN 1 AND 3", name=op.f("ck_external_source_records_trust_tier_allowed")),
        sa.ForeignKeyConstraint(["external_snapshot_id"], ["canonical_v2.external_snapshot_manifests.external_snapshot_id"], name=op.f("fk_external_source_records_external_snapshot_id_external_snapshot_manifests")),
        sa.ForeignKeyConstraint(["artifact_id"], ["canonical_v2.external_raw_artifacts.artifact_id"], name=op.f("fk_external_source_records_artifact_id_external_raw_artifacts")),
        sa.PrimaryKeyConstraint("external_source_record_id", name=op.f("pk_external_source_records")), schema=schema)
    op.create_table("external_holding_records",
        sa.Column("holding_record_id", sa.Text(), nullable=False), sa.Column("external_source_record_id", sa.Text(), nullable=False),
        sa.Column("canonical_source_record_id", sa.Text(), nullable=False), sa.Column("product_source_id", sa.Text(), nullable=False),
        sa.Column("constituent_source_id", sa.Text()), sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("product_resolution_status", sa.String(24), nullable=False), sa.Column("security_resolution_status", sa.String(24), nullable=False),
        sa.Column("normalized_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False), sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("product_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')", name=op.f("ck_external_holding_records_product_resolution_status_allowed")),
        sa.CheckConstraint("security_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED', 'NON_SECURITY')", name=op.f("ck_external_holding_records_security_resolution_status_allowed")),
        sa.ForeignKeyConstraint(["external_source_record_id"], ["canonical_v2.external_source_records.external_source_record_id"], name=op.f("fk_external_holding_records_external_source_record_id_external_source_records")),
        sa.ForeignKeyConstraint(["canonical_source_record_id"], ["canonical_v2.source_records.source_record_id"], name=op.f("fk_external_holding_records_canonical_source_record_id_source_records")),
        sa.PrimaryKeyConstraint("holding_record_id", name=op.f("pk_external_holding_records")),
        sa.UniqueConstraint("canonical_source_record_id", name=op.f("uq_external_holding_records_canonical_source_record_id")), schema=schema)
    op.create_table("holding_fact_details",
        sa.Column("fact_id", sa.Text(), nullable=False), sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("weight_normalized", sa.Numeric(38, 15)), sa.Column("weight_unit", sa.String(64)), sa.Column("weight_scale", sa.String(32)),
        sa.Column("source_provider", sa.Text(), nullable=False), sa.Column("external_holding_record_id", sa.Text(), nullable=False),
        sa.CheckConstraint("weight_normalized IS NULL OR (weight_normalized >= 0 AND weight_normalized <= 1)", name=op.f("ck_holding_fact_details_weight_proportion")),
        sa.CheckConstraint("(weight_normalized IS NULL AND weight_unit IS NULL AND weight_scale IS NULL) OR (weight_normalized IS NOT NULL AND weight_unit IS NOT NULL AND weight_scale IS NOT NULL)", name=op.f("ck_holding_fact_details_weight_semantic_tuple")),
        sa.ForeignKeyConstraint(["fact_id"], ["canonical_v2.entity_relations.fact_id"], name=op.f("fk_holding_fact_details_fact_id_entity_relations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["external_holding_record_id"], ["canonical_v2.external_holding_records.holding_record_id"], name=op.f("fk_holding_fact_details_external_holding_record_id_external_holding_records")),
        sa.PrimaryKeyConstraint("fact_id", name=op.f("pk_holding_fact_details")), schema=schema)
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.9-c2-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))


def downgrade() -> None:
    schema = "canonical_v2"
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.8-b-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))
    # C2 facts cannot satisfy the pre-C2 relation/domain constraints.  Remove
    # only C2-owned canonical rows before restoring those constraints; v1/v2
    # product data and all prior relation families remain untouched.
    op.execute(sa.text(
        "DELETE FROM canonical_v2.canonical_facts f USING canonical_v2.entity_relations r "
        "WHERE f.fact_id = r.fact_id AND r.relation_type IN ('HOLDS', 'SECURITY_ISSUED_BY')"
    ))
    op.drop_table("holding_fact_details", schema=schema)
    op.drop_table("external_holding_records", schema=schema)
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_record_entities "
        "WHERE entity_kind = 'SECURITY' OR source_record_id LIKE 'normalized:holding:%'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_field_assertions "
        "WHERE source_record_id LIKE 'normalized:holding:%'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_records "
        "WHERE source_record_id LIKE 'normalized:holding:%'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.entity_aliases a USING canonical_v2.securities s "
        "WHERE a.entity_id = s.security_id"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.entity_identifiers i USING canonical_v2.securities s "
        "WHERE i.entity_id = s.security_id"
    ))
    op.execute(sa.text("DELETE FROM canonical_v2.securities"))
    op.execute(sa.text("DELETE FROM canonical_v2.canonical_entities WHERE entity_kind = 'SECURITY'"))
    for table in ("external_source_records", "external_raw_artifacts", "external_snapshot_manifests"):
        op.drop_table(table, schema=schema)
    op.execute(sa.text(
        "DELETE FROM canonical_v2.dataset_snapshots "
        "WHERE dataset_id = 'dataset:kodex-holdings'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_datasets "
        "WHERE dataset_id = 'dataset:kodex-holdings'"
    ))
    op.drop_index("ix_securities_isin", table_name="securities", schema=schema)
    op.drop_index("ix_securities_ticker_exchange", table_name="securities", schema=schema)
    op.drop_table("securities", schema=schema)
    op.drop_constraint(op.f("ck_entity_relations_relation_type_allowed"), "entity_relations", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_entity_relations_relation_type_allowed"), "entity_relations",
        "relation_type IN ('HAS_SHARE_CLASS', 'HAS_SALE_LOT', 'MANAGED_BY', 'ISSUED_BY', 'HAS_TRUSTEE', 'HAS_UNDERLYING_INDEX', 'TRACKS_INDEX', 'HAS_BENCHMARK', 'DENOMINATED_IN', 'TRADED_IN_CURRENCY', 'LISTED_IN_COUNTRY', 'HAS_INSTRUMENT_COUNTRY')", schema=schema)
    op.create_unique_constraint("entity_relation_identity", "entity_relations", ["subject_entity_id", "relation_type", "object_entity_id"], schema=schema)
    op.drop_constraint(op.f("ck_source_record_entities_evidence_bearing_kind"), "source_record_entities", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_source_record_entities_evidence_bearing_kind"), "source_record_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT')", schema=schema)
    op.drop_constraint(op.f("ck_canonical_entities_kind_allowed"), "canonical_entities", schema=schema, type_="check")
    op.create_check_constraint(op.f("ck_canonical_entities_kind_allowed"), "canonical_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'ORGANIZATION', 'INDEX', 'CURRENCY', 'COUNTRY')", schema=schema)
