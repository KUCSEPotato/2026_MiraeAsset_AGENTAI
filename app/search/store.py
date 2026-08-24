import json
import math
import sqlite3
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
from app.search.normalization import tokenize


class SemanticIndexError(RuntimeError):
    pass


class SemanticIndexMissingError(SemanticIndexError):
    pass


class SemanticIndexMismatchError(SemanticIndexError):
    pass


class SemanticIndexStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def metadata(self) -> SemanticIndexMetadata:
        if not self._path.is_file():
            raise SemanticIndexMissingError(
                f"semantic index is missing: {self._path}"
            )
        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    "SELECT metadata_json FROM semantic_index_metadata WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            raise SemanticIndexError("semantic index metadata is unreadable") from exc
        if row is None:
            raise SemanticIndexMissingError("semantic index metadata is missing")
        try:
            return SemanticIndexMetadata.model_validate_json(row[0])
        except ValidationError as exc:
            raise SemanticIndexError("semantic index metadata is invalid") from exc

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
        document_tokens = [json.loads(row["tokens_json"]) for row in rows]
        lengths = [len(tokens) for tokens in document_tokens]
        average_length = sum(lengths) / len(lengths)
        document_frequency = {
            term: sum(term in set(tokens) for tokens in document_tokens)
            for term in set(query_terms)
        }
        query_frequency = Counter(query_terms)
        scored: list[tuple[float, sqlite3.Row]] = []
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
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            vector = _unpack_vector(row["vector"], len(query_vector))
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
            with self._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT document_id, text_sha256, vector FROM semantic_documents"
                ).fetchall()
            return {row[0]: (row[1], row[2]) for row in rows}
        except SemanticIndexError:
            return {}

    def _select_rows(
        self,
        filters: dict[str, Any],
        candidate_ids: set[str] | None,
    ) -> list[sqlite3.Row]:
        allowed_columns = {
            "source_dataset",
            "source_field",
            "product_type",
            "region",
            "asset_type",
            "dataset_snapshot",
        }
        clauses: list[str] = []
        parameters: list[Any] = []
        for name, value in filters.items():
            if name not in allowed_columns:
                raise ValueError(f"unsupported semantic metadata filter: {name}")
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            if not values:
                return []
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{name} IN ({placeholders})")
            parameters.extend(values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        statement = "SELECT * FROM semantic_documents" + where
        try:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error as exc:
            raise SemanticIndexError("semantic index search failed") from exc
        if candidate_ids is not None:
            rows = [row for row in rows if row["entity_id"] in candidate_ids]
        return rows

    def _connect(self, *, read_only: bool) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self._path.resolve()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _to_document(row: sqlite3.Row) -> SemanticDocument:
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
            metadata=json.loads(row["metadata_json"]),
        )


def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(value: bytes, dimension: int) -> tuple[float, ...]:
    expected_size = dimension * 4
    if len(value) != expected_size:
        raise SemanticIndexMismatchError(
            f"stored vector has {len(value)} bytes; expected {expected_size}"
        )
    return struct.unpack(f"<{dimension}f", value)
