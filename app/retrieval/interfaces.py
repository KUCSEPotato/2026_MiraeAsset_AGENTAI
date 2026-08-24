from typing import Protocol

from app.domain.models import ExecutionContext, QueryStep, RetrievalRecord


class Retriever(Protocol):
    async def retrieve(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]: ...

