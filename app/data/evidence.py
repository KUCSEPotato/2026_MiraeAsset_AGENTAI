from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select

from app.data.schema import metric_observations, source_field_assertions, source_records


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    canonical_product_id: str
    source_dataset: str
    source_record_id: str
    source_row_number: int
    source_column: str
    raw_value: str | None
    normalized_value: str | None
    snapshot_date: str
    observed_at: str | None
    unit: str | None
    quality_status: str
    transformation_rule: str
    is_missing: bool = False


class EvidenceRepository:
    def for_product(self, connection: Connection, canonical_product_id: str) -> list[FieldEvidence]:
        statement = (
            select(
                source_field_assertions, source_records.c.source_row_number,
                source_records.c.dataset_snapshot, metric_observations.c.observed_at,
                metric_observations.c.unit,
            )
            .join(source_records, source_records.c.source_record_id == source_field_assertions.c.source_record_id)
            .outerjoin(metric_observations,
                       (metric_observations.c.source_record_id == source_field_assertions.c.source_record_id)
                       & (metric_observations.c.source_column == source_field_assertions.c.source_column))
            .where(source_field_assertions.c.canonical_product_id == canonical_product_id)
        )
        return [FieldEvidence(
            canonical_product_id=row.canonical_product_id,
            source_dataset=row.source_dataset, source_record_id=row.source_record_id,
            source_row_number=row.source_row_number, source_column=row.source_column,
            raw_value=row.raw_value, normalized_value=row.normalized_value,
            snapshot_date=row.dataset_snapshot, observed_at=row.observed_at,
            unit=row.unit, quality_status=row.quality_status,
            transformation_rule=row.transformation_rule,
        ) for row in connection.execute(statement)]

    def source_field(
        self,
        connection: Connection,
        *,
        source_record_id: str,
        source_column: str,
        transformation_rule: str,
    ) -> FieldEvidence:
        """Return stored evidence or synthesize an exact MISSING result from RDB payload."""
        assertion = connection.execute(select(source_field_assertions).where(
            source_field_assertions.c.source_record_id == source_record_id,
            source_field_assertions.c.source_column == source_column,
        )).first()
        record = connection.execute(select(source_records).where(
            source_records.c.source_record_id == source_record_id,
        )).one()
        if assertion is not None:
            return FieldEvidence(
                canonical_product_id=assertion.canonical_product_id,
                source_dataset=assertion.source_dataset,
                source_record_id=source_record_id,
                source_row_number=record.source_row_number,
                source_column=source_column, raw_value=assertion.raw_value,
                normalized_value=assertion.normalized_value,
                snapshot_date=record.dataset_snapshot, observed_at=None, unit=None,
                quality_status=assertion.quality_status,
                transformation_rule=assertion.transformation_rule,
            )
        raw_value = record.raw_payload.get(source_column)
        normalized_value = record.normalized_payload.get(source_column)
        if normalized_value is not None:
            raise LookupError("present source field has no assertion")
        return FieldEvidence(
            canonical_product_id=record.canonical_product_id,
            source_dataset=record.dataset_id, source_record_id=source_record_id,
            source_row_number=record.source_row_number, source_column=source_column,
            raw_value=None if raw_value is None else str(raw_value), normalized_value=None,
            snapshot_date=record.dataset_snapshot, observed_at=None, unit=None,
            quality_status="MISSING", transformation_rule=transformation_rule,
            is_missing=True,
        )
