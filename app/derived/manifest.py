"""Compatibility identity for stores projected from canonical_v2.

Derived stores deliberately carry their source snapshot identity.  Existence
of an index file or Neo4j database is never evidence that it is compatible
with the selected canonical snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DerivedStoreStatus(StrEnum):
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"


class DerivedStoreManifest(BaseModel):
    store_kind: str
    status: DerivedStoreStatus
    generation: str
    snapshot: str
    ontology_version: str
    canonical_schema_version: str
    transformer_version: str
    projection_version: str
    built_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    entity_counts: dict[str, int] = Field(default_factory=dict)
    relation_counts: dict[str, int] = Field(default_factory=dict)
    document_count: int = 0
    checksum: str = ""
    validation: dict[str, Any] = Field(default_factory=dict)

    def assert_compatible(
        self,
        *,
        generation: str,
        snapshot: str,
        ontology_version: str,
        canonical_schema_version: str,
        transformer_version: str,
        projection_version: str,
    ) -> None:
        expected = {
            "generation": generation,
            "snapshot": snapshot,
            "ontology_version": ontology_version,
            "canonical_schema_version": canonical_schema_version,
            "transformer_version": transformer_version,
            "projection_version": projection_version,
        }
        mismatches = [
            f"{name}={getattr(self, name)!r} expected {value!r}"
            for name, value in expected.items()
            if getattr(self, name) != value
        ]
        if self.status is not DerivedStoreStatus.READY:
            mismatches.insert(0, f"status={self.status.value!r} expected 'READY'")
        if mismatches:
            raise ValueError("incompatible derived-store manifest: " + "; ".join(mismatches))
