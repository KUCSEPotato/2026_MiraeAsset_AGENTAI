from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SemanticDocument(BaseModel):
    """One source-preserving searchable field linked to a canonical entity."""

    document_id: str
    entity_id: str
    source_dataset: str
    source_record_key: str
    source_field: str
    raw_text: str
    normalized_text: str
    product_type: str
    region: str | None = None
    asset_type: str | None = None
    dataset_snapshot: str
    observed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticIndexMetadata(BaseModel):
    index_version: str
    dataset_snapshot: str
    embedding_model: str
    embedding_dimension: int
    indexed_at: datetime
    source_rows: int
    document_count: int
    skipped_missing: int
    duplicate_texts: int
    average_document_length: float


class SemanticSearchHit(BaseModel):
    document: SemanticDocument
    score: float
    rank: int


class IndexBuildResult(BaseModel):
    metadata: SemanticIndexMetadata
    reused_embeddings: int = 0
    generated_embeddings: int = 0
