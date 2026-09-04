"""Canonical fields declared by an ontology-capable runtime.

Ontology validation checks declarations across both repository generations. A
field being recognized here does not make it executable by the legacy RDB
compiler; each compiler continues to enforce its own field registry.
"""

from app.retrieval.rdb import RDBFieldRegistry
from app.retrieval.rdb_v2 import CanonicalV2FieldRegistry


LEGACY_CANONICAL_FIELDS = RDBFieldRegistry().canonical_fields
CANONICAL_V2_FIELDS = CanonicalV2FieldRegistry().canonical_fields
ONTOLOGY_CANONICAL_FIELDS = LEGACY_CANONICAL_FIELDS | CANONICAL_V2_FIELDS
