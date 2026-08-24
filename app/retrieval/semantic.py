import asyncio
from typing import Any

from app.domain.models import (
    ExecutionContext,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
    StepExecutionStatus,
)
from app.retrieval.exceptions import RetrieverUnavailableError
from app.search.config import SearchSettings
from app.search.embedding import EmbeddingProvider
from app.search.models import SemanticSearchHit
from app.search.store import SemanticIndexError, SemanticIndexStore


class RealBM25Retriever:
    def __init__(
        self,
        store: SemanticIndexStore,
        settings: SearchSettings,
        *,
        snapshot_date: str,
    ) -> None:
        self._store = store
        self._settings = settings
        self._snapshot = snapshot_date

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        query = _query_text(step)
        if not query:
            return []
        candidate_ids = _candidate_ids(step, context)
        if candidate_ids == set():
            return []
        try:
            await asyncio.to_thread(self._validate_index)
            hits = await asyncio.to_thread(
                self._store.bm25_search,
                query,
                top_k=_top_k(step, self._settings.bm25_top_k),
                filters=_metadata_filters(step, self._snapshot),
                candidate_ids=candidate_ids,
            )
        except SemanticIndexError as exc:
            raise RetrieverUnavailableError(str(exc)) from exc
        return [_to_record(hit, RetrievalSource.BM25, step.step_id) for hit in hits]

    def _validate_index(self) -> None:
        self._store.validate(
            snapshot=self._snapshot,
            index_version=self._settings.index_version,
            embedding_model=self._settings.embedding_model,
            embedding_dimension=self._settings.embedding_dimension,
        )


class RealVectorRetriever:
    def __init__(
        self,
        store: SemanticIndexStore,
        embedding_provider: EmbeddingProvider,
        settings: SearchSettings,
        *,
        snapshot_date: str,
    ) -> None:
        self._store = store
        self._embedding = embedding_provider
        self._settings = settings
        self._snapshot = snapshot_date

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        query = _query_text(step)
        if not query:
            return []
        candidate_ids = _candidate_ids(step, context)
        if candidate_ids == set():
            return []
        try:
            await asyncio.to_thread(self._validate_index)
        except SemanticIndexError as exc:
            raise RetrieverUnavailableError(str(exc)) from exc
        try:
            query_vector = await self._embedding.embed_query(query)
        except Exception as exc:
            raise RetrieverUnavailableError("query embedding failed") from exc
        if len(query_vector) != self._embedding.dimension:
            raise ValueError("embedding provider returned the wrong dimension")
        try:
            hits = await asyncio.to_thread(
                self._store.vector_search,
                query_vector,
                top_k=_top_k(step, self._settings.vector_top_k),
                filters=_metadata_filters(step, self._snapshot),
                candidate_ids=candidate_ids,
            )
        except SemanticIndexError as exc:
            raise RetrieverUnavailableError(str(exc)) from exc
        return [_to_record(hit, RetrievalSource.VECTOR, step.step_id) for hit in hits]

    def _validate_index(self) -> None:
        self._store.validate(
            snapshot=self._snapshot,
            index_version=self._settings.index_version,
            embedding_model=self._embedding.model_name,
            embedding_dimension=self._embedding.dimension,
        )


def _query_text(step: QueryStep) -> str:
    values = step.inputs.get("query_terms") or step.inputs.get("entity_mentions") or []
    if not isinstance(values, list):
        raise ValueError("semantic query terms must be a list")
    return " ".join(str(value).strip() for value in values if str(value).strip())


def _top_k(step: QueryStep, configured: int) -> int:
    value = step.inputs.get("top_k", configured)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("semantic top_k must be a positive integer")
    return min(value, configured)


def _metadata_filters(step: QueryStep, snapshot: str) -> dict[str, Any]:
    filters = dict(step.inputs.get("metadata_filters", {}))
    if "entity_mentions" in step.inputs:
        filters.setdefault("source_field", ["product.name"])
    filters.setdefault("dataset_snapshot", snapshot)
    return filters


def _candidate_ids(
    step: QueryStep,
    context: ExecutionContext,
) -> set[str] | None:
    explicit = step.inputs.get("candidate_ids")
    if explicit is not None:
        if not isinstance(explicit, list):
            raise ValueError("candidate_ids must be a list")
        return {str(value) for value in explicit}
    dependency_ids = step.inputs.get("candidate_ids_from", [])
    if not isinstance(dependency_ids, list):
        raise ValueError("candidate_ids_from must be a list")
    if not dependency_ids:
        return None
    candidates: set[str] = set()
    for dependency_id in dependency_ids:
        result = context.step_results.get(dependency_id)
        if result is None or result.status is not StepExecutionStatus.SUCCESS:
            continue
        candidates.update(
            record.entity_id
            for record in result.records
            if record.entity_id is not None
        )
    return candidates


def _to_record(
    hit: SemanticSearchHit,
    source: RetrievalSource,
    step_id: str,
) -> RetrievalRecord:
    document = hit.document
    score_name = "bm25_score" if source is RetrievalSource.BM25 else "cosine_similarity"
    return RetrievalRecord(
        step_id=step_id,
        source=source.value,
        source_id=document.document_id,
        entity_id=document.entity_id,
        payload={
            "field": document.source_field,
            "value": document.raw_text,
            "text": document.raw_text,
        },
        metadata={
            **document.metadata,
            "source_dataset": document.source_dataset,
            "source_record_key": document.source_record_key,
            "source_field": document.source_field,
            "dataset_snapshot": document.dataset_snapshot,
            "observed_at": document.observed_at,
            "retrieval_source": source.value,
            "retrieval_score": hit.score,
            "retrieval_score_type": score_name,
            "retrieval_rank": hit.rank,
        },
    )
