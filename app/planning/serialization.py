from typing import Any

from app.domain.models import (
    ConceptCategory,
    ConstraintSemanticType,
    FilterOperator,
    GroundedQuery,
    ResolutionStatus,
)


def structured_query_inputs(query: GroundedQuery) -> dict[str, Any]:
    product_concepts = [
        concept.canonical_concept
        for concept in query.grounded_concepts
        if concept.category is ConceptCategory.PRODUCT_TYPE
        and concept.canonical_concept is not None
        and concept.raw_text in query.parsed_query.product_types
    ]
    resolved_entities = [
        entity
        for entity in query.resolved_entities
        if entity.resolution_status is ResolutionStatus.RESOLVED
        and entity.canonical_id is not None
    ]
    result_grain = _result_grain(resolved_entities)
    return {
        # Physical compatibility keys are isolated at this planner/compiler
        # boundary. Ontology identity remains available alongside them.
        "product_types": [concept.value for concept in product_concepts],
        "ontology_product_types": [
            {
                "ontology_uri": getattr(concept, "ontology_uri", None),
                "canonical_name": getattr(
                    concept, "canonical_name", concept.value
                ),
                "runtime_key": concept.value,
            }
            for concept in product_concepts
        ],
        "filters": [
            {
                "raw": item.raw_filter.model_dump(
                    mode="json", exclude={"constraint_id"}
                ),
                "canonical_field": item.canonical_field,
                "canonical_value": _filter_value(item),
            }
            for item in query.grounded_filters
        ],
        "sort": [
            {
                "raw": item.raw_sort.model_dump(
                    mode="json", exclude={"constraint_id"}
                ),
                "canonical_field": item.canonical_field,
            }
            for item in query.grounded_sort
        ],
        "requested_fields": [
            item.canonical_field
            for item in query.grounded_requested_fields
            if item.canonical_field is not None
        ],
        "requested_field_constraint_ids": [
            item.constraint_id
            for item in query.semantic_constraints
            if item.semantic_type is ConstraintSemanticType.REQUESTED_FIELD
        ],
        "entity_ids": [
            entity.canonical_id
            for entity in resolved_entities
            if entity.entity_type in {"product", "fund_share_class", "sale_lot"}
        ],
        "entity_constraint_ids": [
            entity.constraint_id
            for entity in query.resolved_entities
            if entity.constraint_id is not None
        ],
        "result_limit": (
            query.parsed_query.result_limit.value
            if query.parsed_query.result_limit is not None
            else None
        ),
        "limit_constraint_id": (
            query.parsed_query.result_limit.constraint_id
            if query.parsed_query.result_limit is not None
            else None
        ),
        **({"result_grain": result_grain} if result_grain is not None else {}),
    }


def has_structured_inputs(inputs: dict[str, Any]) -> bool:
    return any(
        inputs.get(key)
        for key in (
            "product_types",
            "filters",
            "sort",
            "requested_fields",
            "entity_ids",
        )
    )


def _filter_value(item) -> Any:
    if item.raw_filter.operator is FilterOperator.IN:
        if item.canonical_values:
            return [value.value for value in item.canonical_values]
        return _normalized_raw_value(
            item.raw_filter.model_dump(mode="json")["value"]
        )
    if item.canonical_value is not None:
        return item.canonical_value.value
    raw = item.raw_filter.model_dump(mode="json")["value"]
    return _normalized_raw_value(raw)


def _normalized_raw_value(value: Any) -> Any:
    if isinstance(value, dict) and "normalized" in value:
        return value["normalized"]
    if isinstance(value, list):
        return [_normalized_raw_value(item) for item in value]
    return value


def _result_grain(resolved_entities) -> str | None:
    """Preserve an explicitly resolved non-product entity grain end-to-end."""
    kinds = {entity.entity_type for entity in resolved_entities}
    if kinds == {"fund_share_class"}:
        return "fund_share_class"
    if kinds == {"sale_lot"}:
        return "sale_lot"
    return None
