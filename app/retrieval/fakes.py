from app.domain.models import (
    ExecutionContext,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
)


class _DeterministicFakeRetriever:
    source: RetrievalSource
    entity_ids: tuple[str, ...]

    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        return [
            RetrievalRecord(
                source=self.source.value,
                source_id=f"fake-{self.source.value}-{entity_id}",
                entity_id=entity_id,
                payload={
                    "field": "pipeline_status",
                    "value": f"deterministic_{self.source.value}_test_record",
                    "text": (
                        f"M5 {self.source.value} test evidence only; "
                        "this is not financial product data."
                    ),
                },
                metadata={
                    "fake": True,
                    "dataset_snapshot": "2026-07-11",
                    "retriever": type(self).__name__,
                },
            )
            for entity_id in self.entity_ids
        ]


class FakeRDBRetriever(_DeterministicFakeRetriever):
    source = RetrievalSource.RDB
    entity_ids = ("fake-product-shared", "fake-product-rdb-only")


class FakeGraphRetriever(_DeterministicFakeRetriever):
    source = RetrievalSource.GRAPH
    entity_ids = ("fake-product-shared", "fake-product-graph-only")


class FakeVectorRetriever(_DeterministicFakeRetriever):
    source = RetrievalSource.VECTOR
    entity_ids = ("fake-product-shared", "fake-product-vector-only")


class FakeBM25Retriever(_DeterministicFakeRetriever):
    source = RetrievalSource.BM25
    entity_ids = ("fake-product-shared", "fake-product-bm25-only")

