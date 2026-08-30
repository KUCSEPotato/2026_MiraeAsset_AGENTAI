"""M10.8-B clean rebuild support

Revision ID: 8c21f3a1b7d4
Revises: fd9910efb66e
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c21f3a1b7d4"
down_revision: Union[str, Sequence[str], None] = "fd9910efb66e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_canonical_entities_kind_allowed"),
        "canonical_entities",
        schema="canonical_v2",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_canonical_entities_kind_allowed"),
        "canonical_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'ORGANIZATION', 'INDEX', 'CURRENCY', 'COUNTRY')",
        schema="canonical_v2",
    )
    for name in (
        "generation",
        "ontology_version",
        "semantic_mapping_version",
        "transformer_version",
        "database_schema_version",
    ):
        op.add_column(
            "dataset_snapshots",
            sa.Column(
                name,
                sa.String(length=64 if name != "generation" else 16),
                nullable=False,
                server_default=sa.text("'UNSPECIFIED'"),
            ),
            schema="canonical_v2",
        )
    op.add_column(
        "identity_resolution_cases",
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        schema="canonical_v2",
    )
    op.alter_column(
        "entity_id_crosswalk",
        "v2_entity_id",
        existing_type=sa.Text(),
        nullable=True,
        schema="canonical_v2",
    )
    op.drop_constraint(
        op.f("ck_entity_id_crosswalk_mapping_status_allowed"),
        "entity_id_crosswalk",
        schema="canonical_v2",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_entity_id_crosswalk_mapping_status_allowed"),
        "entity_id_crosswalk",
        "mapping_status IN ('EXACT', 'SPLIT', 'MERGED', 'AMBIGUOUS', 'REJECTED', 'RETIRED')",
        schema="canonical_v2",
    )
    op.create_check_constraint(
        op.f("ck_entity_id_crosswalk_retired_has_no_v2_target"),
        "entity_id_crosswalk",
        "(mapping_status = 'RETIRED' AND v2_entity_id IS NULL) OR (mapping_status <> 'RETIRED' AND v2_entity_id IS NOT NULL)",
        schema="canonical_v2",
    )
    op.drop_constraint(
        op.f("ck_canonical_facts_fact_kind_allowed"),
        "canonical_facts",
        schema="canonical_v2",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_canonical_facts_fact_kind_allowed"),
        "canonical_facts",
        "fact_kind IN ('SCALAR', 'ENTITY_RELATION', 'ORGANIZATION_RELATION', 'INDEX_RELATION', 'CLASSIFICATION', 'METRIC')",
        schema="canonical_v2",
    )
    op.create_table(
        "canonical_scalar_facts",
        sa.Column("fact_id", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("numeric_value", sa.Numeric(38, 15), nullable=True),
        sa.Column("date_value", sa.Date(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "value_type IN ('TEXT', 'NUMERIC', 'DATE', 'BOOLEAN')",
            name=op.f("ck_canonical_scalar_facts_value_type_allowed"),
        ),
        sa.CheckConstraint(
            "(value_type = 'TEXT' AND text_value IS NOT NULL AND numeric_value IS NULL AND date_value IS NULL AND boolean_value IS NULL) OR "
            "(value_type = 'NUMERIC' AND text_value IS NULL AND numeric_value IS NOT NULL AND date_value IS NULL AND boolean_value IS NULL) OR "
            "(value_type = 'DATE' AND text_value IS NULL AND numeric_value IS NULL AND date_value IS NOT NULL AND boolean_value IS NULL) OR "
            "(value_type = 'BOOLEAN' AND text_value IS NULL AND numeric_value IS NULL AND date_value IS NULL AND boolean_value IS NOT NULL)",
            name=op.f("ck_canonical_scalar_facts_exactly_one_typed_value"),
        ),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["canonical_v2.canonical_facts.fact_id"],
            name="fk_canonical_scalar_facts_fact_id_canonical_facts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("fact_id", name="pk_canonical_scalar_facts"),
        schema="canonical_v2",
    )
    op.create_table(
        "entity_relations",
        sa.Column("fact_id", sa.Text(), nullable=False),
        sa.Column("subject_entity_id", sa.Text(), nullable=False),
        sa.Column("relation_type", sa.String(length=40), nullable=False),
        sa.Column("object_entity_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "relation_type IN ('HAS_SHARE_CLASS', 'HAS_SALE_LOT', 'MANAGED_BY', 'ISSUED_BY', 'HAS_TRUSTEE', 'HAS_UNDERLYING_INDEX', 'TRACKS_INDEX', 'HAS_BENCHMARK', 'DENOMINATED_IN', 'TRADED_IN_CURRENCY', 'LISTED_IN_COUNTRY', 'HAS_INSTRUMENT_COUNTRY')",
            name=op.f("ck_entity_relations_relation_type_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["canonical_v2.canonical_facts.fact_id"],
            name="fk_entity_relations_fact_id_canonical_facts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_entity_id"],
            ["canonical_v2.canonical_entities.entity_id"],
            name="fk_entity_relations_object_entity_id_canonical_entities",
        ),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["canonical_v2.canonical_entities.entity_id"],
            name="fk_entity_relations_subject_entity_id_canonical_entities",
        ),
        sa.PrimaryKeyConstraint("fact_id", name="pk_entity_relations"),
        sa.UniqueConstraint(
            "subject_entity_id",
            "relation_type",
            "object_entity_id",
            name="entity_relation_identity",
        ),
        schema="canonical_v2",
    )
    op.create_index(
        "ix_entity_relations_target",
        "entity_relations",
        ["object_entity_id", "relation_type"],
        unique=False,
        schema="canonical_v2",
    )
    op.execute(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.8-b-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.8-a-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    )
    op.drop_index(
        "ix_entity_relations_target",
        table_name="entity_relations",
        schema="canonical_v2",
    )
    op.drop_table("entity_relations", schema="canonical_v2")
    op.drop_table("canonical_scalar_facts", schema="canonical_v2")
    # A downgrade intentionally removes B-only materialization.  Delete the
    # evidence links and parent facts before restoring A's fact-kind and
    # entity-kind allowlists so a populated B snapshot can be downgraded
    # deterministically instead of failing constraint validation.
    op.execute(
        "DELETE FROM canonical_v2.fact_evidence_links WHERE fact_id IN ("
        "SELECT fact_id FROM canonical_v2.canonical_facts "
        "WHERE fact_kind IN ('SCALAR', 'ENTITY_RELATION'))"
    )
    op.execute(
        "DELETE FROM canonical_v2.canonical_facts "
        "WHERE fact_kind IN ('SCALAR', 'ENTITY_RELATION')"
    )
    op.execute(
        "DELETE FROM canonical_v2.canonical_entities "
        "WHERE entity_kind IN ('CURRENCY', 'COUNTRY')"
    )
    op.drop_constraint(
        op.f("ck_canonical_entities_kind_allowed"),
        "canonical_entities",
        schema="canonical_v2",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_canonical_entities_kind_allowed"),
        "canonical_entities",
        "entity_kind IN ('FINANCIAL_PRODUCT', 'FUND_SHARE_CLASS', 'SALE_LOT', 'ORGANIZATION', 'INDEX')",
        schema="canonical_v2",
    )
    op.drop_constraint(
        op.f("ck_canonical_facts_fact_kind_allowed"),
        "canonical_facts",
        schema="canonical_v2",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_entity_id_crosswalk_retired_has_no_v2_target"),
        "entity_id_crosswalk",
        schema="canonical_v2",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_entity_id_crosswalk_mapping_status_allowed"),
        "entity_id_crosswalk",
        schema="canonical_v2",
        type_="check",
    )
    op.execute(
        "DELETE FROM canonical_v2.entity_id_crosswalk "
        "WHERE mapping_status = 'RETIRED' OR v2_entity_id IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_entity_id_crosswalk_mapping_status_allowed"),
        "entity_id_crosswalk",
        "mapping_status IN ('EXACT', 'SPLIT', 'MERGED', 'AMBIGUOUS', 'REJECTED')",
        schema="canonical_v2",
    )
    op.alter_column(
        "entity_id_crosswalk",
        "v2_entity_id",
        existing_type=sa.Text(),
        nullable=False,
        schema="canonical_v2",
    )
    op.create_check_constraint(
        op.f("ck_canonical_facts_fact_kind_allowed"),
        "canonical_facts",
        "fact_kind IN ('ORGANIZATION_RELATION', 'INDEX_RELATION', 'CLASSIFICATION', 'METRIC')",
        schema="canonical_v2",
    )
    op.drop_column(
        "identity_resolution_cases",
        "reason_code",
        schema="canonical_v2",
    )
    for name in reversed(
        (
            "generation",
            "ontology_version",
            "semantic_mapping_version",
            "transformer_version",
            "database_schema_version",
        )
    ):
        op.drop_column("dataset_snapshots", name, schema="canonical_v2")
