"""M10.9-C3 source-to-canonical external metric bridge.

Revision ID: 4a9f21c8d730
Revises: 7e4c2a9d8f10
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "4a9f21c8d730"
down_revision: Union[str, Sequence[str], None] = "7e4c2a9d8f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_metric_records",
        sa.Column("metric_observation_id", sa.Text(), nullable=False),
        sa.Column("external_source_record_id", sa.Text(), nullable=False),
        sa.Column("canonical_source_record_id", sa.Text(), nullable=False),
        sa.Column("product_source_id", sa.Text(), nullable=False),
        sa.Column("metric_code", sa.String(64), nullable=False),
        sa.Column("observation_end_date", sa.Date(), nullable=False),
        sa.Column("product_resolution_status", sa.String(24), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "product_resolution_status IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')",
            name=op.f(
                "ck_external_metric_records_product_resolution_status_allowed"
            ),
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name=op.f("ck_external_metric_records_payload_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["external_source_record_id"],
            ["canonical_v2.external_source_records.external_source_record_id"],
            name=op.f(
                "fk_external_metric_records_external_source_record_id_external_source_records"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_source_record_id"],
            ["canonical_v2.source_records.source_record_id"],
            name=op.f(
                "fk_external_metric_records_canonical_source_record_id_source_records"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "metric_observation_id",
            name=op.f("pk_external_metric_records"),
        ),
        sa.UniqueConstraint(
            "canonical_source_record_id",
            name=op.f(
                "uq_external_metric_records_canonical_source_record_id"
            ),
        ),
        schema="canonical_v2",
    )
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.9-c3-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))


def downgrade() -> None:
    snapshot_id = "snapshot:ishares-us-one-year-return:20260824:v1"
    op.execute(sa.text(
        "DELETE FROM canonical_v2.canonical_facts "
        f"WHERE snapshot_id = '{snapshot_id}'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_record_entities "
        f"WHERE source_record_id IN (SELECT source_record_id FROM "
        "canonical_v2.source_records "
        f"WHERE snapshot_id = '{snapshot_id}')"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_field_assertions "
        f"WHERE source_record_id IN (SELECT source_record_id FROM "
        "canonical_v2.source_records "
        f"WHERE snapshot_id = '{snapshot_id}')"
    ))
    op.execute(sa.text("DELETE FROM canonical_v2.external_metric_records"))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_records "
        f"WHERE snapshot_id = '{snapshot_id}'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_source_records WHERE external_snapshot_id IN "
        "(SELECT external_snapshot_id FROM canonical_v2.external_snapshot_manifests "
        f"WHERE canonical_snapshot_id = '{snapshot_id}')"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_raw_artifacts WHERE external_snapshot_id IN "
        "(SELECT external_snapshot_id FROM canonical_v2.external_snapshot_manifests "
        f"WHERE canonical_snapshot_id = '{snapshot_id}')"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_snapshot_manifests "
        f"WHERE canonical_snapshot_id = '{snapshot_id}'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.dataset_snapshots "
        f"WHERE snapshot_id = '{snapshot_id}'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_datasets "
        "WHERE dataset_id = 'dataset:ishares-us-performance'"
    ))
    op.drop_table("external_metric_records", schema="canonical_v2")
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.9-c2.6-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))
