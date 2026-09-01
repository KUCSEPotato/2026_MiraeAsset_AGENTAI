"""M10.9-C2.6 authoritative Security issuer source bridge.

Revision ID: 7e4c2a9d8f10
Revises: c29f4d8a6102
Create Date: 2026-08-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "7e4c2a9d8f10"
down_revision: Union[str, Sequence[str], None] = "c29f4d8a6102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "canonical_v2"
    status = "IN ('RESOLVED', 'AMBIGUOUS', 'CONFLICT', 'UNRESOLVED')"
    op.create_table(
        "external_security_issuer_records",
        sa.Column("issuer_record_id", sa.Text(), nullable=False),
        sa.Column("external_source_record_id", sa.Text(), nullable=False),
        sa.Column("canonical_source_record_id", sa.Text(), nullable=False),
        sa.Column("security_ticker", sa.Text(), nullable=False),
        sa.Column("security_source_id", sa.Text(), nullable=False),
        sa.Column("issuer_source_id", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("security_identity_status", sa.String(24), nullable=False),
        sa.Column("issuer_identity_status", sa.String(24), nullable=False),
        sa.Column("relation_validation_status", sa.String(24), nullable=False),
        sa.Column(
            "normalized_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            f"security_identity_status {status}",
            name=op.f("ck_external_security_issuer_records_security_identity_status_allowed"),
        ),
        sa.CheckConstraint(
            f"issuer_identity_status {status}",
            name=op.f("ck_external_security_issuer_records_issuer_identity_status_allowed"),
        ),
        sa.CheckConstraint(
            f"relation_validation_status {status}",
            name=op.f("ck_external_security_issuer_records_relation_validation_status_allowed"),
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name=op.f("ck_external_security_issuer_records_payload_sha256_length"),
        ),
        sa.ForeignKeyConstraint(
            ["external_source_record_id"],
            ["canonical_v2.external_source_records.external_source_record_id"],
            name=op.f(
                "fk_external_security_issuer_records_external_source_record_id_external_source_records"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["canonical_source_record_id"],
            ["canonical_v2.source_records.source_record_id"],
            name=op.f(
                "fk_external_security_issuer_records_canonical_source_record_id_source_records"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "issuer_record_id",
            name=op.f("pk_external_security_issuer_records"),
        ),
        sa.UniqueConstraint(
            "canonical_source_record_id",
            name=op.f(
                "uq_external_security_issuer_records_canonical_source_record_id"
            ),
        ),
        schema=schema,
    )
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.9-c2.6-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))


def downgrade() -> None:
    # Remove only C2.6-owned canonical/provenance rows.  This makes a
    # populated downgrade return to the accepted C2 holdings state rather
    # than leaving issuer facts or source-record foreign keys behind.
    op.execute(sa.text(
        "DELETE FROM canonical_v2.canonical_facts "
        "WHERE snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.entity_aliases "
        "WHERE source_record_id LIKE 'normalized:issuerrec\\_%' ESCAPE '\\'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.entity_identifiers "
        "WHERE source_record_id LIKE 'normalized:issuerrec\\_%' ESCAPE '\\'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_record_entities "
        "WHERE source_record_id LIKE 'normalized:issuerrec\\_%' ESCAPE '\\'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_field_assertions "
        "WHERE source_record_id LIKE 'normalized:issuerrec\\_%' ESCAPE '\\'"
    ))
    op.execute(sa.text("DELETE FROM canonical_v2.external_security_issuer_records"))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_records "
        "WHERE snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_source_records WHERE external_snapshot_id IN ("
        "SELECT external_snapshot_id FROM canonical_v2.external_snapshot_manifests "
        "WHERE canonical_snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1')"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_raw_artifacts WHERE external_snapshot_id IN ("
        "SELECT external_snapshot_id FROM canonical_v2.external_snapshot_manifests "
        "WHERE canonical_snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1')"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.external_snapshot_manifests "
        "WHERE canonical_snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.dataset_snapshots "
        "WHERE snapshot_id = 'snapshot:krx-kind-security-issuer:20260824:v1'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.source_datasets "
        "WHERE dataset_id = 'dataset:krx-kind-security-issuer'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.organizations "
        "WHERE organization_id LIKE 'organization:krx-kind:%'"
    ))
    op.execute(sa.text(
        "DELETE FROM canonical_v2.canonical_entities "
        "WHERE entity_id LIKE 'organization:krx-kind:%'"
    ))
    op.drop_table("external_security_issuer_records", schema="canonical_v2")
    op.execute(sa.text(
        "UPDATE canonical_v2.schema_versions "
        "SET version = 'm10.9-c2-canonical-v2', installed_at = CURRENT_TIMESTAMP "
        "WHERE component = 'canonical_data_model'"
    ))
