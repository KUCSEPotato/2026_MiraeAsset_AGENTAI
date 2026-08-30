import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from app.search.config import SearchSettings
from app.search.documents import ForeignETFStrategyDocumentBuilder
from app.search.embedding import EmbeddingProvider
from app.search.models import IndexBuildResult, SemanticIndexMetadata
from app.search.normalization import tokenize
from app.search.store import (
    SEMANTIC_ARTIFACT_FORMAT,
    SemanticIndexStore,
    encode_vector,
    pack_vector,
)
from app.derived.manifest import DerivedStoreManifest


class SemanticIndexBuilder:
    def __init__(
        self,
        document_builder: ForeignETFStrategyDocumentBuilder,
        embedding_provider: EmbeddingProvider,
        settings: SearchSettings,
        derived_manifest_factory=None,
    ) -> None:
        self._documents = document_builder
        self._embedding = embedding_provider
        self._settings = settings
        self._derived_manifest_factory = derived_manifest_factory

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
        previous_document_ids = existing_store.document_ids(
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
        derived_manifest = (
            self._derived_manifest_factory(len(documents), metadata)
            if self._derived_manifest_factory is not None
            else None
        )
        if derived_manifest is not None:
            metadata = metadata.model_copy(
                update={
                    "generation": derived_manifest.generation,
                    "ontology_version": derived_manifest.ontology_version,
                    "canonical_schema_version": derived_manifest.canonical_schema_version,
                    "transformer_version": derived_manifest.transformer_version,
                }
            )
        self._write_atomically(documents, text_hashes, vectors, metadata, derived_manifest)
        return IndexBuildResult(
            metadata=metadata,
            reused_embeddings=reused,
            generated_embeddings=len(pending),
            new_documents=len(set(text_hashes) - previous_document_ids),
            removed_documents=len(previous_document_ids - set(text_hashes)),
            regenerated_embeddings=sum(
                document.document_id in previous_document_ids
                for document in pending
            ),
        )

    @property
    def _settings_snapshot(self) -> str:
        snapshot = self._documents.snapshot_date
        if not snapshot:
            raise ValueError("document builder snapshot is unavailable")
        return snapshot

    def _write_atomically(self, documents, text_hashes, vectors, metadata, derived_manifest: DerivedStoreManifest | None = None) -> None:
        target = self._settings.index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
            mode="w",
            encoding="utf-8",
        )
        temporary_path = handle.name
        try:
            artifact = {
                "format": SEMANTIC_ARTIFACT_FORMAT,
                "metadata": metadata.model_dump(mode="json"),
                "documents": [
                    {
                        **document.model_dump(mode="json"),
                        "tokens": tokenize(document.normalized_text),
                        "text_sha256": text_hashes[document.document_id],
                        "vector": encode_vector(vectors[document.document_id]),
                    }
                    for document in documents
                ],
            }
            if derived_manifest is not None:
                artifact["derived_manifest"] = derived_manifest.model_dump(mode="json")
            json.dump(
                artifact,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(temporary_path, target)
        finally:
            if not handle.closed:
                handle.close()
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
