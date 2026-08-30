import base64
import json
import math
import struct
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.search.models import (
    SemanticDocument,
    SemanticIndexMetadata,
    SemanticSearchHit,
)
from app.derived.manifest import DerivedStoreManifest
from app.search.normalization import tokenize


SEMANTIC_ARTIFACT_FORMAT = "semantic-search-json-v1"


class SemanticIndexError(RuntimeError):
    pass


class SemanticIndexMissingError(SemanticIndexError):
    pass


class SemanticIndexMismatchError(SemanticIndexError):
    pass


class SemanticIndexStore:
    """Read a derived Vector/BM25 artifact built from PostgreSQL facts."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def metadata(self) -> SemanticIndexMetadata:
        artifact = self._load_artifact()
        try:
            return SemanticIndexMetadata.model_validate(artifact["metadata"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise SemanticIndexError("semantic index metadata is invalid") from exc

    def derived_manifest(self) -> DerivedStoreManifest:
        artifact = self._load_artifact()
        try:
            return DerivedStoreManifest.model_validate(artifact["derived_manifest"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise SemanticIndexMismatchError(
                "semantic index is not a canonical_v2 derived artifact"
            ) from exc

    def validate_derived_manifest(
        self,
        *,
        generation: str,
        snapshot: str,
        ontology_version: str,
        canonical_schema_version: str,
        transformer_version: str,
        projection_version: str,
    ) -> DerivedStoreManifest:
        try:
            manifest = self.derived_manifest()
            manifest.assert_compatible(
                generation=generation,
                snapshot=snapshot,
                ontology_version=ontology_version,
                canonical_schema_version=canonical_schema_version,
                transformer_version=transformer_version,
                projection_version=projection_version,
            )
            return manifest
        except ValueError as exc:
            raise SemanticIndexMismatchError(str(exc)) from exc

    def validate(
        self,
        *,
        snapshot: str,
        index_version: str,
        embedding_model: str,
        embedding_dimension: int,
    ) -> SemanticIndexMetadata:
        metadata = self.metadata()
        mismatches = {
            "dataset_snapshot": (metadata.dataset_snapshot, snapshot),
            "index_version": (metadata.index_version, index_version),
            "embedding_model": (metadata.embedding_model, embedding_model),
            "embedding_dimension": (
                metadata.embedding_dimension,
                embedding_dimension,
            ),
        }
        invalid = [
            f"{name}={actual!r} expected {expected!r}"
            for name, (actual, expected) in mismatches.items()
            if actual != expected
        ]
        if invalid:
            raise SemanticIndexMismatchError(
                "stale or incompatible semantic index: " + "; ".join(invalid)
            )
        return metadata

    def bm25_search(
        self,
        query: str,
        *,
        top_k: int,
        filters: dict[str, Any],
        candidate_ids: set[str] | None = None,
    ) -> list[SemanticSearchHit]:
        query_terms = tokenize(query)
        if not query_terms:
            return []
        rows = self._select_rows(filters, candidate_ids)
        if not rows:
            return []
        document_tokens = [row["tokens"] for row in rows]
        lengths = [len(tokens) for tokens in document_tokens]
        average_length = sum(lengths) / len(lengths)
        document_frequency = {
            term: sum(term in set(tokens) for tokens in document_tokens)
            for term in set(query_terms)
        }
        query_frequency = Counter(query_terms)
        scored: list[tuple[float, dict[str, Any]]] = []
        k1 = 1.5
        b = 0.75
        corpus_size = len(rows)
        for row, tokens, document_length in zip(
            rows, document_tokens, lengths, strict=True
        ):
            term_frequency = Counter(tokens)
            score = 0.0
            for term, query_count in query_frequency.items():
                frequency = term_frequency[term]
                if frequency == 0:
                    continue
                df = document_frequency[term]
                inverse_document_frequency = math.log(
                    1.0 + (corpus_size - df + 0.5) / (df + 0.5)
                )
                denominator = frequency + k1 * (
                    1.0 - b + b * document_length / average_length
                )
                score += (
                    inverse_document_frequency
                    * frequency
                    * (k1 + 1.0)
                    / denominator
                    * query_count
                )
            if score > 0.0:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["document_id"]))
        return [
            SemanticSearchHit(
                document=self._to_document(row),
                score=score,
                rank=rank,
            )
            for rank, (score, row) in enumerate(scored[:top_k], start=1)
        ]

    def vector_search(
        self,
        query_vector: list[float],
        *,
        top_k: int,
        filters: dict[str, Any],
        candidate_ids: set[str] | None = None,
    ) -> list[SemanticSearchHit]:
        rows = self._select_rows(filters, candidate_ids)
        if not rows or not any(query_vector):
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            vector = _unpack_vector(_decode_vector(row), len(query_vector))
            score = sum(left * right for left, right in zip(query_vector, vector))
            scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["document_id"]))
        return [
            SemanticSearchHit(
                document=self._to_document(row),
                score=score,
                rank=rank,
            )
            for rank, (score, row) in enumerate(scored[:top_k], start=1)
        ]

    def embedding_cache(
        self,
        *,
        snapshot: str,
        index_version: str,
        embedding_model: str,
        embedding_dimension: int,
    ) -> dict[str, tuple[str, bytes]]:
        try:
            self.validate(
                snapshot=snapshot,
                index_version=index_version,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            rows = self._load_artifact()["documents"]
            return {
                row["document_id"]: (row["text_sha256"], _decode_vector(row))
                for row in rows
            }
        except (KeyError, TypeError, SemanticIndexError):
            return {}

    def document_ids(
        self,
        *,
        snapshot: str,
        index_version: str,
        embedding_model: str,
        embedding_dimension: int,
    ) -> set[str]:
        try:
            self.validate(
                snapshot=snapshot,
                index_version=index_version,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            return {
                str(row["document_id"])
                for row in self._load_artifact()["documents"]
            }
        except (KeyError, TypeError, SemanticIndexError):
            return set()

    def _select_rows(
        self,
        filters: dict[str, Any],
        candidate_ids: set[str] | None,
    ) -> list[dict[str, Any]]:
        allowed_columns = {
            "source_dataset",
            "source_field",
            "product_type",
            "region",
            "asset_type",
            "dataset_snapshot",
        }
        normalized_filters: dict[str, list[Any]] = {}
        for name, value in filters.items():
            if name not in allowed_columns:
                raise ValueError(f"unsupported semantic metadata filter: {name}")
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            if not values:
                return []
            normalized_filters[name] = values
        try:
            rows = self._load_artifact()["documents"]
        except (KeyError, TypeError) as exc:
            raise SemanticIndexError("semantic index documents are invalid") from exc
        return [
            row
            for row in rows
            if all(
                row.get(name) in values
                for name, values in normalized_filters.items()
            )
            and (candidate_ids is None or row.get("entity_id") in candidate_ids)
        ]

    def _load_artifact(self) -> dict[str, Any]:
        if not self._path.is_file():
            raise SemanticIndexMissingError(
                f"semantic index is missing: {self._path}"
            )
        try:
            artifact = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SemanticIndexError("semantic index artifact is unreadable") from exc
        if (
            not isinstance(artifact, dict)
            or artifact.get("format") != SEMANTIC_ARTIFACT_FORMAT
        ):
            raise SemanticIndexError("semantic index artifact format is unsupported")
        if not isinstance(artifact.get("documents"), list):
            raise SemanticIndexError("semantic index documents are invalid")
        return artifact

    @staticmethod
    def _to_document(row: dict[str, Any]) -> SemanticDocument:
        return SemanticDocument(
            document_id=row["document_id"],
            entity_id=row["entity_id"],
            source_dataset=row["source_dataset"],
            source_record_key=row["source_record_key"],
            source_field=row["source_field"],
            raw_text=row["raw_text"],
            normalized_text=row["normalized_text"],
            product_type=row["product_type"],
            region=row["region"],
            asset_type=row["asset_type"],
            dataset_snapshot=row["dataset_snapshot"],
            observed_at=row["observed_at"],
            metadata=row["metadata"],
        )


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def encode_vector(vector: bytes) -> str:
    return base64.b64encode(vector).decode("ascii")


def _decode_vector(row: dict[str, Any]) -> bytes:
    try:
        return base64.b64decode(row["vector"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise SemanticIndexError("semantic index vector is invalid") from exc


def _unpack_vector(value: bytes, dimension: int) -> tuple[float, ...]:
    expected_size = dimension * 4
    if len(value) != expected_size:
        raise SemanticIndexMismatchError(
            f"stored vector has {len(value)} bytes; expected {expected_size}"
        )
    return struct.unpack(f"<{dimension}f", value)
