import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SearchSettings:
    index_path: Path = Path("data/semantic_search.json")
    index_version: str = "m9-strategy-v1"
    embedding_model: str = "multilingual-semantic-hash-v1"
    embedding_dimension: int = 384
    bm25_top_k: int = 10
    vector_top_k: int = 10
    candidate_limit: int = 10_000
    v2_index_path: Path = Path("data/canonical_v2/semantic_search.json")
    v2_index_version: str = "m10.9-c2-canonical-v2-semantic-1"
    v2_generation: str = "260824"
    v2_ontology_version: str = "merged-optical-1.4"
    v2_transformer_version: str = "m10.9-c2-kodex-holdings-1"

    @classmethod
    def from_env(cls) -> "SearchSettings":
        settings = cls(
            index_path=Path(
                os.getenv("SEMANTIC_INDEX_PATH", "data/semantic_search.json")
            ),
            index_version=os.getenv(
                "SEMANTIC_INDEX_VERSION", "m10.7-strategy-20260829"
            ),
            embedding_model=os.getenv(
                "EMBEDDING_MODEL", "multilingual-semantic-hash-v1"
            ),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "384")),
            bm25_top_k=int(os.getenv("BM25_TOP_K", "10")),
            vector_top_k=int(os.getenv("VECTOR_TOP_K", "10")),
            candidate_limit=int(os.getenv("SEMANTIC_CANDIDATE_LIMIT", "10000")),
            v2_index_path=Path(os.getenv("CANONICAL_V2_SEMANTIC_INDEX_PATH", "data/canonical_v2/semantic_search.json")),
            v2_index_version=os.getenv("CANONICAL_V2_SEMANTIC_INDEX_VERSION", "m10.9-c2-canonical-v2-semantic-1"),
            v2_generation=os.getenv("CANONICAL_V2_GENERATION", "260824"),
            v2_ontology_version=os.getenv("CANONICAL_V2_ONTOLOGY_VERSION", "merged-optical-1.4"),
            v2_transformer_version=os.getenv("CANONICAL_V2_TRANSFORMER_VERSION", "m10.9-c2-kodex-holdings-1"),
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
