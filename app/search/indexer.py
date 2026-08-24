import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from app.search.config import SearchSettings
from app.search.documents import ForeignETFStrategyDocumentBuilder
from app.search.embedding import EmbeddingProvider
from app.search.models import IndexBuildResult, SemanticIndexMetadata
from app.search.normalization import tokenize
from app.search.store import SemanticIndexStore, pack_vector


class SemanticIndexBuilder:
    def __init__(
        self,
        document_builder: ForeignETFStrategyDocumentBuilder,
        embedding_provider: EmbeddingProvider,
        settings: SearchSettings,
    ) -> None:
        self._documents = document_builder
        self._embedding = embedding_provider
        self._settings = settings

    async def build(self) -> IndexBuildResult:
        documents, stats = self._documents.build()
        if any(
            document.dataset_snapshot != self._settings_snapshot
            for document in documents
        ):
            raise ValueError("document builder returned mixed dataset snapshots")

        existing_store = SemanticIndexStore(self._settings.index_path)
        cache = existing_store.embedding_cache(
            snapshot=self._settings_snapshot,
            index_version=self._settings.index_version,
            embedding_model=self._embedding.model_name,
            embedding_dimension=self._embedding.dimension,
        )
        text_hashes = {
            document.document_id: hashlib.sha256(
                document.normalized_text.encode("utf-8")
            ).hexdigest()
            for document in documents
        }
        vectors: dict[str, bytes] = {}
        pending = []
        reused = 0
        for document in documents:
            cached = cache.get(document.document_id)
            if cached is not None and cached[0] == text_hashes[document.document_id]:
                vectors[document.document_id] = cached[1]
                reused += 1
            else:
                pending.append(document)
        for offset in range(0, len(pending), 256):
            batch = pending[offset : offset + 256]
            embedded = await self._embedding.embed_documents(
                [document.normalized_text for document in batch]
            )
            if len(embedded) != len(batch):
                raise ValueError("embedding provider returned the wrong batch size")
            for document, vector in zip(batch, embedded, strict=True):
                if len(vector) != self._embedding.dimension:
                    raise ValueError("embedding provider returned the wrong dimension")
                vectors[document.document_id] = pack_vector(vector)

        metadata = SemanticIndexMetadata(
            index_version=self._settings.index_version,
            dataset_snapshot=self._settings_snapshot,
            embedding_model=self._embedding.model_name,
            embedding_dimension=self._embedding.dimension,
            indexed_at=datetime.now(timezone.utc),
            source_rows=stats.source_rows,
            document_count=len(documents),
            skipped_missing=stats.skipped_missing + stats.skipped_sentinel,
            duplicate_texts=stats.duplicate_texts,
            average_document_length=(
                sum(len(document.raw_text) for document in documents) / len(documents)
                if documents
                else 0.0
            ),
        )
        self._write_atomically(documents, text_hashes, vectors, metadata)
        return IndexBuildResult(
            metadata=metadata,
            reused_embeddings=reused,
            generated_embeddings=len(pending),
        )

    @property
    def _settings_snapshot(self) -> str:
        snapshot = self._documents.snapshot_date
        if not snapshot:
            raise ValueError("document builder snapshot is unavailable")
        return snapshot

    def _write_atomically(self, documents, text_hashes, vectors, metadata) -> None:
        target = self._settings.index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
        )
        temporary_path = handle.name
        handle.close()
        try:
            with sqlite3.connect(temporary_path) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO semantic_index_metadata"
                    "(id, metadata_json) VALUES(1, ?)",
                    (metadata.model_dump_json(),),
                )
                connection.executemany(
                    """
                    INSERT INTO semantic_documents(
                        document_id, entity_id, source_dataset, source_record_key,
                        source_field, raw_text, normalized_text, product_type,
                        region, asset_type, dataset_snapshot, observed_at,
                        metadata_json, tokens_json, text_sha256, vector
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document.document_id,
                            document.entity_id,
                            document.source_dataset,
                            document.source_record_key,
                            document.source_field,
                            document.raw_text,
                            document.normalized_text,
                            document.product_type,
                            document.region,
                            document.asset_type,
                            document.dataset_snapshot,
                            document.observed_at,
                            json.dumps(document.metadata, ensure_ascii=False),
                            json.dumps(tokenize(document.normalized_text)),
                            text_hashes[document.document_id],
                            vectors[document.document_id],
                        )
                        for document in documents
                    ],
                )
                connection.commit()
            os.replace(temporary_path, target)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


_SCHEMA = """
CREATE TABLE semantic_index_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    metadata_json TEXT NOT NULL
);
CREATE TABLE semantic_documents (
    document_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    source_dataset TEXT NOT NULL,
    source_record_key TEXT NOT NULL,
    source_field TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    product_type TEXT NOT NULL,
    region TEXT,
    asset_type TEXT,
    dataset_snapshot TEXT NOT NULL,
    observed_at TEXT,
    metadata_json TEXT NOT NULL,
    tokens_json TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE INDEX ix_semantic_entity ON semantic_documents(entity_id);
CREATE INDEX ix_semantic_source ON semantic_documents(source_dataset, source_field);
CREATE INDEX ix_semantic_product_type ON semantic_documents(product_type);
CREATE INDEX ix_semantic_region ON semantic_documents(region);
CREATE INDEX ix_semantic_asset_type ON semantic_documents(asset_type);
CREATE INDEX ix_semantic_snapshot ON semantic_documents(dataset_snapshot);
"""
