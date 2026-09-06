"""User-facing labels derived only from already retrieved evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.models import Evidence, EvidenceBundle, ResolutionStatus


_RISK_GRADE_PATTERN = re.compile(r"RiskGrade\.([1-6])")


def format_evidence_value(field: str | None, value: str) -> str:
    """Format a canonical value without adding interpretation or ordering."""
    if field == "product.risk_grade":
        matched = _RISK_GRADE_PATTERN.fullmatch(value)
        if matched:
            return f"{matched.group(1)}등급"
    return value


def evidence_entity_labels(items: Iterable[Evidence]) -> dict[str, str]:
    """Choose the best available human-readable label for each entity."""
    grouped: dict[str, list[Evidence]] = {}
    for item in items:
        if item.entity_id:
            grouped.setdefault(item.entity_id, []).append(item)
    return {
        entity_id: _best_label(entity_id, evidence)
        for entity_id, evidence in grouped.items()
    }


def bundle_entity_labels(bundle: EvidenceBundle) -> dict[str, str]:
    """Add resolved query aliases only when retrieved evidence has no name."""
    labels = evidence_entity_labels(bundle.evidence)
    for entity in bundle.resolved_entities:
        if (
            entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.canonical_id
            and entity.raw_text.strip()
        ):
            labels.setdefault(entity.canonical_id, entity.raw_text.strip())
    return labels


def _best_label(entity_id: str, items: list[Evidence]) -> str:
    # Canonical/display names supplied by a retriever have highest priority.
    for key in ("display_name", "canonical_name", "preferred_name"):
        if label := _metadata_label(items, key, entity_id):
            return label

    # A projected canonical name is authoritative even if older retrievers did
    # not attach a separate preferred-name metadata field.
    for item in items:
        if item.field == "product.name" and (
            label := _human_label(item.value, entity_id)
        ):
            return label

    # Preserve source product labels used by legacy/semantic adapters.
    for key in ("source_display_name", "source_product_name", "product_name"):
        if label := _metadata_label(items, key, entity_id):
            return label
    for item in items:
        if item.source_type == "rdb" and (
            label := _human_label(item.text, entity_id)
        ):
            return label

    # Short human-readable identifiers are preferable to an internal ID.
    for field in ("product.ticker", "product.short_name"):
        for item in items:
            if item.field == field and (
                label := _human_label(item.value, entity_id)
            ):
                return label
    return entity_id


def _metadata_label(items: list[Evidence], key: str, entity_id: str) -> str | None:
    for item in items:
        if label := _human_label(item.metadata.get(key), entity_id):
            return label
    return None


def _human_label(value: object, entity_id: str) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    return label if label and label != entity_id else None
