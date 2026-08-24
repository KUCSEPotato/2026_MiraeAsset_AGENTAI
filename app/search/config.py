import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchSettings:
    index_path: Path = Path("data/semantic_search.db")
    index_version: str = "m9-strategy-v1"
    embedding_model: str = "multilingual-semantic-hash-v1"
    embedding_dimension: int = 384
    bm25_top_k: int = 10
    vector_top_k: int = 10
    candidate_limit: int = 10_000

    @classmethod
    def from_env(cls) -> "SearchSettings":
        settings = cls(
            index_path=Path(
                os.getenv("SEMANTIC_INDEX_PATH", "data/semantic_search.db")
            ),
            index_version=os.getenv(
                "SEMANTIC_INDEX_VERSION", "m9-strategy-v1"
            ),
            embedding_model=os.getenv(
                "EMBEDDING_MODEL", "multilingual-semantic-hash-v1"
            ),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "384")),
            bm25_top_k=int(os.getenv("BM25_TOP_K", "10")),
            vector_top_k=int(os.getenv("VECTOR_TOP_K", "10")),
            candidate_limit=int(os.getenv("SEMANTIC_CANDIDATE_LIMIT", "10000")),
        )
        for name, value in (
            ("EMBEDDING_DIMENSION", settings.embedding_dimension),
            ("BM25_TOP_K", settings.bm25_top_k),
            ("VECTOR_TOP_K", settings.vector_top_k),
            ("SEMANTIC_CANDIDATE_LIMIT", settings.candidate_limit),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        return settings
