import hashlib
import math
from typing import Protocol

from app.search.normalization import normalize_text, tokenize


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class DeterministicMultilingualEmbeddingProvider:
    """Offline M9 baseline with explicit Korean/English semantic features.

    This is a reproducible feature embedding rather than a neural model. The
    provider boundary allows a multilingual neural model to replace it without
    changing the index or retriever contracts.
    """

    _SUPPORTED_MODEL = "multilingual-semantic-hash-v1"
    _CONCEPT_ALIASES = {
        "artificial_intelligence": (
            "artificial intelligence",
            "machine learning",
            "deep learning",
            "인공지능",
            "머신러닝",
            "딥러닝",
            "ai 기술",
            "ai 산업",
            "ai 기업",
        ),
        "semiconductor": ("semiconductor", "semiconductors", "반도체"),
        "us_treasury": (
            "u.s. treasury",
            "us treasury",
            "united states treasury",
            "treasury securities",
            "미국 국채",
            "미 국채",
        ),
        "gold": ("physical gold", "gold bullion", "gold", "금 현물", "금"),
        "covered_call": (
            "covered call",
            "call writing",
            "buy-write",
            "커버드콜",
            "커버드 콜",
        ),
        "healthcare": (
            "healthcare",
            "health care",
            "medical technology",
            "헬스케어",
            "의료",
        ),
        "clean_energy": (
            "clean energy",
            "renewable energy",
            "solar energy",
            "친환경 에너지",
            "재생 에너지",
            "청정 에너지",
        ),
        "robotics": ("robotics", "automation", "로봇", "자동화"),
        "cybersecurity": ("cybersecurity", "cyber security", "사이버 보안"),
        "blockchain": ("blockchain", "digital assets", "블록체인"),
        "technology": ("technology companies", "technology sector", "기술 기업"),
        "income": ("income generation", "current income", "인컴", "수익 창출"),
    }

    def __init__(
        self,
        *,
        model_name: str = _SUPPORTED_MODEL,
        dimension: int = 384,
    ) -> None:
        if model_name != self._SUPPORTED_MODEL:
            raise ValueError(f"unsupported deterministic embedding model: {model_name}")
        if dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        self._model_name = model_name
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        normalized = normalize_text(text)
        features: list[tuple[str, float]] = []
        tokens = tokenize(normalized)
        features.extend((f"token:{token}", 1.0) for token in tokens)
        features.extend(
            (f"bigram:{left}_{right}", 0.65)
            for left, right in zip(tokens, tokens[1:], strict=False)
        )
        for concept, aliases in self._CONCEPT_ALIASES.items():
            if any(alias in normalized for alias in aliases):
                features.append((f"concept:{concept}", 4.0))
        vector = [0.0] * self._dimension
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "little") % self._dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector
