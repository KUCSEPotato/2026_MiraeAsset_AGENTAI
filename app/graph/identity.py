import hashlib

from app.data.cleaning import normalize_lookup_value


def source_scoped_name_id(
    node_type: str,
    source_dataset: str,
    label: str,
) -> str:
    normalized = normalize_lookup_value(label)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"{_identity_type(node_type)}:{source_dataset}:{digest}"


def explicit_source_id(
    node_type: str,
    source_dataset: str,
    identifier: str,
) -> str:
    return f"{_identity_type(node_type)}:{source_dataset}:{identifier.strip()}"


def canonical_concept_id(node_type: str, canonical_value: str) -> str:
    return f"{node_type.casefold()}:{canonical_value}"


def graph_edge_id(
    subject_id: str,
    edge_type: str,
    object_id: str,
    snapshot: str,
) -> str:
    raw = "|".join((subject_id, edge_type, object_id, snapshot))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _identity_type(node_type: str) -> str:
    return node_type.casefold().replace("_", "").replace("-", "")
