import asyncio
from typing import Any

from app.domain.models import (
    ExecutionContext,
    QueryStep,
    RetrievalRecord,
    RetrievalResult,
    RetrievalSource,
    StepExecutionStatus,
)
from app.retrieval.exceptions import RetrieverUnavailableError
from app.search.config import SearchSettings
from app.search.embedding import EmbeddingProvider
from app.search.models import SemanticSearchHit
from app.search.store import SemanticIndexError, SemanticIndexStore
from app.data.v2_schema import CANONICAL_V2_SCHEMA_VERSION


class RealBM25Retriever:
    def __init__(
        self,
        store: SemanticIndexStore,
        settings: SearchSettings,
        *,
        snapshot_date: str,
        canonical_v2: bool = False,
    ) -> None:
        self._store = store
        self._settings = settings
        self._snapshot = snapshot_date
        self._canonical_v2 = canonical_v2

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        return (await self.retrieve_with_result(step, context)).records

    async def retrieve_with_result(
        self, step: QueryStep, context: ExecutionContext,
    ) -> RetrievalResult:
        query = _query_text(step)
        if not query:
            return _search_result([], complete=True)
        candidate_ids = _candidate_ids(step, context)
        if candidate_ids == set():
            return _search_result([], complete=True)
        limit = _top_k(
            step, self._settings.candidate_limit
            if step.inputs.get("require_complete_candidates") else self._settings.bm25_top_k,
        )
        try:
            await asyncio.to_thread(self._validate_index)
            hits = await asyncio.to_thread(
                self._store.bm25_search,
                query,
                top_k=limit + 1,
                filters=_metadata_filters(step, self._snapshot, canonical_v2=self._canonical_v2),
                candidate_ids=candidate_ids,
            )
        except SemanticIndexError as exc:
            raise RetrieverUnavailableError(str(exc)) from exc
        records = [_to_record(
            hit, RetrievalSource.BM25, step.step_id,
            generation=self._settings.v2_generation if self._canonical_v2 else None,
        ) for hit in hits[:limit]]
        return _search_result(records, complete=len(hits) <= limit, limit=limit)

    def _validate_index(self) -> None:
        self._store.validate(
            snapshot=self._snapshot,
            index_version=(self._settings.v2_index_version if self._canonical_v2 else self._settings.index_version),
            embedding_model=self._settings.embedding_model,
            embedding_dimension=self._settings.embedding_dimension,
        )
        if self._canonical_v2:
            self._store.validate_derived_manifest(
                generation=self._settings.v2_generation, snapshot=self._snapshot,
                ontology_version=self._settings.v2_ontology_version,
                canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
                transformer_version=self._settings.v2_transformer_version,
                projection_version=self._settings.v2_index_version,
            )


class RealVectorRetriever:
    def __init__(
        self,
        store: SemanticIndexStore,
        embedding_provider: EmbeddingProvider,
        settings: SearchSettings,
        *,
        snapshot_date: str,
        canonical_v2: bool = False,
    ) -> None:
        self._store = store
        self._embedding = embedding_provider
        self._settings = settings
        self._snapshot = snapshot_date
        self._canonical_v2 = canonical_v2

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        return (await self.retrieve_with_result(step, context)).records

    async def retrieve_with_result(
        self, step: QueryStep, context: ExecutionContext,
    ) -> RetrievalResult:
        query = _query_text(step)
        if not query:
            return _search_result([], complete=True)
        candidate_ids = _candidate_ids(step, context)
        if candidate_ids == set():
            return _search_result([], complete=True)
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
                filters=_metadata_filters(step, self._snapshot, canonical_v2=self._canonical_v2),
                candidate_ids=candidate_ids,
            )
        except SemanticIndexError as exc:
            raise RetrieverUnavailableError(str(exc)) from exc
        records = [_to_record(
            hit, RetrievalSource.VECTOR, step.step_id,
            generation=self._settings.v2_generation if self._canonical_v2 else None,
        ) for hit in hits]
        # A relevance window has no reviewed boolean matching threshold. It
        # cannot claim a complete universe for subsequent financial ranking.
        return _search_result(records, complete=not hits)

    def _validate_index(self) -> None:
        self._store.validate(
            snapshot=self._snapshot,
            index_version=(self._settings.v2_index_version if self._canonical_v2 else self._settings.index_version),
            embedding_model=self._embedding.model_name,
            embedding_dimension=self._embedding.dimension,
        )
        if self._canonical_v2:
            self._store.validate_derived_manifest(
                generation=self._settings.v2_generation, snapshot=self._snapshot,
                ontology_version=self._settings.v2_ontology_version,
                canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
                transformer_version=self._settings.v2_transformer_version,
                projection_version=self._settings.v2_index_version,
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


def _metadata_filters(step: QueryStep, snapshot: str, *, canonical_v2: bool = False) -> dict[str, Any]:
    filters = dict(step.inputs.get("metadata_filters", {}))
    if canonical_v2:
        # The v2 corpus is deliberately restricted to approved foreign ETP
        # strategy assertions; structured constraints remain candidate IDs.
        filters["source_dataset"] = ["PREF02N001"]
        filters["source_field"] = ["source.cu_strtegy"]
        product_types = filters.get("product_type")
        if product_types is not None:
            values = product_types if isinstance(product_types, list) else [product_types]
            filters["product_type"] = [
                str(value).rsplit(".", 1)[-1].upper() for value in values
            ]
        # Region and asset-class strings in a v1 semantic document are not a
        # canonical_v2 classification source.  Their hard filtering is carried
        # by the preceding canonical_v2 RDB candidate set, never approximated
        # by vector metadata.
        filters.pop("region", None)
        filters.pop("asset_type", None)
    elif "entity_mentions" in step.inputs:
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
    candidates: list[set[str]] = []
    for dependency_id in dependency_ids:
        if dependency_id not in step.depends_on:
            raise RetrieverUnavailableError("semantic candidate is not a declared dependency")
        result = context.step_results.get(dependency_id)
        if result is None or result.status is not StepExecutionStatus.SUCCESS:
            raise RetrieverUnavailableError("semantic candidate dependency is incomplete")
        candidates.append({
            record.entity_id
            for record in result.records
            if record.entity_id is not None
        })
    return set.intersection(*candidates)


def _to_record(
    hit: SemanticSearchHit,
    source: RetrievalSource,
    step_id: str,
    *,
    generation: str | None = None,
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
            **({"generation": generation, "snapshot_identity": f"{generation}:{document.dataset_snapshot}"}
               if generation is not None else {}),
            "observed_at": document.observed_at,
            "retrieval_source": source.value,
            "retrieval_score": hit.score,
            "retrieval_score_type": score_name,
            "retrieval_rank": hit.rank,
        },
    )


def _search_result(records, *, complete: bool, limit: int | None = None) -> RetrievalResult:
    return RetrievalResult(
        records=records,
        total_matches=len(records) if complete else None,
        returned_count=len(records),
        window_limit=limit,
        counts={"candidate_set_complete": int(complete)},
    )
