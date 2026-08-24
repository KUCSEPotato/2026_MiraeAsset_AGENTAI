from app.domain.models import RetrievalSource
from app.retrieval.exceptions import RetrieverNotRegisteredError
from app.retrieval.interfaces import Retriever


class RetrieverRegistry:
    def __init__(
        self,
        retrievers: dict[RetrievalSource, Retriever] | None = None,
    ) -> None:
        self._retrievers: dict[RetrievalSource, Retriever] = {}
        for source, retriever in (retrievers or {}).items():
            self.register(source, retriever)

    def register(self, source: RetrievalSource, retriever: Retriever) -> None:
        if source is RetrievalSource.INTERNAL:
            raise ValueError("internal transforms are not retrievers")
        self._retrievers[source] = retriever

    def get(self, source: RetrievalSource) -> Retriever:
        try:
            return self._retrievers[source]
        except KeyError as exc:
            raise RetrieverNotRegisteredError(
                f"retriever is not registered for source: {source.value}"
            ) from exc
