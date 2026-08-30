"""canonical_v2 semantic-document projection.

Only approved descriptive source assertions with a resolved described entity
are indexed.  The source assertion supplies text; canonical_v2 supplies the
identity and product grain.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, func, select

from app.data.v2_schema import (
    CANONICAL_V2_SCHEMA_VERSION,
    canonical_entities,
    dataset_snapshots,
    financial_products,
    source_datasets,
    source_field_assertions,
    source_record_entities,
    source_records,
)
from app.derived.manifest import DerivedStoreManifest, DerivedStoreStatus
from app.graph.v2 import V2_TRANSFORMER_VERSION, v2_manifest_checksum
from app.search.documents import DocumentBuildStats, _SENTINELS
from app.search.models import SemanticDocument, SemanticIndexMetadata
from app.search.normalization import normalize_text


V2_SEMANTIC_PROJECTION_VERSION = "m10.9-c2-canonical-v2-semantic-1"
V2_SEMANTIC_DATASET = "PREF02N001"
V2_STRATEGY_COLUMN = "cu_strtegy"


class CanonicalV2StrategyDocumentBuilder:
    """Foreign ETP strategy documents linked to canonical_v2 products only."""

    def __init__(self, engine: Engine, *, snapshot_ids: tuple[str, ...], snapshot_date: str) -> None:
        if not snapshot_ids:
            raise ValueError("canonical_v2 semantic index requires snapshot ids")
        self._engine = engine
        self._snapshot_ids = snapshot_ids
        self._snapshot_date = snapshot_date

    @property
    def snapshot_date(self) -> str:
        return self._snapshot_date

    def build(self) -> tuple[list[SemanticDocument], DocumentBuildStats]:
        statement = (
            select(
                source_field_assertions.c.assertion_id,
                source_field_assertions.c.raw_value,
                source_records.c.source_record_id,
                source_records.c.source_primary_key,
                source_records.c.source_row_number,
                canonical_entities.c.entity_id,
                canonical_entities.c.preferred_name,
                financial_products.c.product_type_code,
                dataset_snapshots.c.snapshot_date,
            )
            .select_from(
                source_field_assertions
                .join(source_records, source_records.c.source_record_id == source_field_assertions.c.source_record_id)
                .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == source_records.c.snapshot_id)
                .join(source_datasets, source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id)
                .join(source_record_entities, (source_record_entities.c.source_record_id == source_records.c.source_record_id) & (source_record_entities.c.provenance_role == "DESCRIBES"))
                .join(canonical_entities, canonical_entities.c.entity_id == source_record_entities.c.entity_id)
                .join(financial_products, financial_products.c.product_id == canonical_entities.c.entity_id)
            )
            .where(
                source_records.c.snapshot_id.in_(self._snapshot_ids),
                source_datasets.c.dataset_code == V2_SEMANTIC_DATASET,
                source_field_assertions.c.source_column == V2_STRATEGY_COLUMN,
            )
            .order_by(source_records.c.source_record_id)
        )
        count_statement = (
            select(func.count())
            .select_from(
                source_field_assertions
                .join(source_records, source_records.c.source_record_id == source_field_assertions.c.source_record_id)
                .join(dataset_snapshots, dataset_snapshots.c.snapshot_id == source_records.c.snapshot_id)
                .join(source_datasets, source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id)
            )
            .where(
                source_records.c.snapshot_id.in_(self._snapshot_ids),
                source_datasets.c.dataset_code == V2_SEMANTIC_DATASET,
                source_field_assertions.c.source_column == V2_STRATEGY_COLUMN,
            )
        )
        with self._engine.connect() as connection:
            source_rows = int(connection.scalar(count_statement) or 0)
            rows = connection.execute(statement).mappings().all()
        documents: list[SemanticDocument] = []
        skipped_missing = source_rows - len(rows)
        skipped_sentinel = 0
        normalized_counts: dict[str, int] = {}
        for row in rows:
            raw = row["raw_value"]
            if raw is None or not str(raw).strip():
                skipped_missing += 1
                continue
            text = str(raw).strip()
            normalized = normalize_text(text)
            if normalized in _SENTINELS:
                skipped_sentinel += 1
                continue
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
            record_id = str(row["source_record_id"])
            entity_id = str(row["entity_id"])
            documents.append(SemanticDocument(
                document_id=f"v2:{record_id}:{V2_STRATEGY_COLUMN}",
                entity_id=entity_id,
                source_dataset=V2_SEMANTIC_DATASET,
                source_record_key=record_id,
                source_field="source.cu_strtegy",
                raw_text=text,
                normalized_text=normalized,
                product_type=str(row["product_type_code"]),
                dataset_snapshot=str(row["snapshot_date"]),
                metadata={
                    "canonical_entity_id": entity_id,
                    "source_record_id": record_id,
                    "source_primary_key": row["source_primary_key"],
                    "source_row_number": row["source_row_number"],
                    "source_assertion_id": row["assertion_id"],
                    "text_role": "strategy_description",
                    "canonical_product_name": row["preferred_name"],
                    "canonical_v2": True,
                },
            ))
        return documents, DocumentBuildStats(
            source_rows=source_rows,
            skipped_missing=skipped_missing,
            skipped_sentinel=skipped_sentinel,
            duplicate_texts=sum(count - 1 for count in normalized_counts.values() if count > 1),
        )


def v2_semantic_manifest_factory(
    *, generation: str, snapshot: str, ontology_version: str, projection_version: str = V2_SEMANTIC_PROJECTION_VERSION
):
    def create(document_count: int, metadata: SemanticIndexMetadata) -> DerivedStoreManifest:
        entities = {"SemanticDocument": document_count}
        return DerivedStoreManifest(
            store_kind="semantic_index",
            status=DerivedStoreStatus.READY,
            generation=generation,
            snapshot=snapshot,
            ontology_version=ontology_version,
            canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
            transformer_version=V2_TRANSFORMER_VERSION,
            projection_version=projection_version,
            entity_counts=entities,
            document_count=document_count,
            checksum=v2_manifest_checksum(entities, {}, document_count=document_count),
            validation={"source": "canonical_v2", "corpus": "PREF02N001.cu_strtegy", "bm25_vector_corpus": "identical"},
        )
    return create
