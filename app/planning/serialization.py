from typing import Any

from app.domain.models import (
    ConceptCategory,
    ConstraintSemanticType,
    FilterOperator,
    GroundedQuery,
    ResolutionStatus,
    OrderedComparison,
    SortOperation,
    TopN,
    QueryIntent,
)
from app.planning.predicates import structured_predicate


BASIC_PRODUCT_PROJECTION = "BASIC_PRODUCT"
BASIC_PRODUCT_FIELDS = (
    "product.name",
    "product.product_type",
    "product.ticker",
    "product.isin",
)


def structured_query_inputs(query: GroundedQuery) -> dict[str, Any]:
    predicate = structured_predicate(query)
    comparison_fields = list(dict.fromkeys(
        item.canonical_field for item in query.grounded_requested_fields
        if item.canonical_field is not None
    ))
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
    explicit_requested_fields = [
        item.canonical_field
        for item in query.grounded_requested_fields
        if item.canonical_field is not None
    ]
    basic_product_lookup = bool(
        len(resolved_entities) == 1
        and resolved_entities[0].entity_type == "product"
        and not query.grounded_requested_fields
        and not query.grounded_filters
        and not query.grounded_sort
        and not query.grounded_relations
        and not query.parsed_query.requires_semantic_search
        and not query.parsed_query.semantic_terms
    )
    fund_subscription_fields = {
        "product.current_fund_subscription_eligible",
        "product.subscription_status",
        "product.latest_fund_price_available",
    }
    result_grain = (
        "fund_share_class"
        if any(item.canonical_field in fund_subscription_fields for item in query.grounded_filters)
        else _result_grain(resolved_entities)
    )
    return {
        "boolean_expression": predicate.model_dump(mode="json") if predicate else None,
        "comparison": (
            {"mode": "fieldwise", "fields": comparison_fields or [item.canonical_field for item in query.grounded_sort if item.canonical_field]}
            if query.parsed_query.comparison is not None
            or query.parsed_query.intent is QueryIntent.COMPARE_PRODUCTS
            else None
        ),
        "metrics": [metric.model_dump(mode="json") for metric in query.parsed_query.metrics],
        # Physical compatibility keys are isolated at this planner/compiler
        # boundary. Ontology identity remains available alongside them.
        "product_types": [concept.value for concept in product_concepts],
        "product_universe": (
            {
                "operation": "UNION",
                "operands": query.parsed_query.product_universe.operands,
            }
            if query.parsed_query.product_universe is not None
            else None
        ),
        "product_universe_constraint_id": (
            query.parsed_query.product_universe.constraint_id
            if query.parsed_query.product_universe is not None
            else None
        ),
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
        "requested_fields": (
            list(BASIC_PRODUCT_FIELDS)
            if basic_product_lookup
            else explicit_requested_fields
        ),
        "requested_field_details": [
            {
                "raw": item.raw_text,
                "canonical_field": item.canonical_field,
            }
            for item in query.grounded_requested_fields
            if item.canonical_field is not None
        ],
        "projection_profile": (
            BASIC_PRODUCT_PROJECTION if basic_product_lookup else None
        ),
        "filter_constraint_ids": [
            item.raw_filter.constraint_id for item in query.grounded_filters
        ],
        "sort_constraint_ids": [
            item.raw_sort.constraint_id for item in query.grounded_sort
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
        "sort_operations": [
            SortOperation(
                semantic_metric_key=item.canonical_field,
                direction=item.raw_sort.direction,
            ).model_dump(mode="json")
            for item in query.grounded_sort
            if item.canonical_field is not None
        ],
        "top_n": (
            TopN(value=query.parsed_query.result_limit.value).model_dump(mode="json")
            if query.parsed_query.result_limit is not None
            and query.parsed_query.result_limit.value <= 1000
            and (
                bool(query.parsed_query.sort)
                or query.parsed_query.result_limit.raw_text.startswith(("상위", "가장"))
            )
            else None
        ),
        "ordered_comparisons": [
            OrderedComparison(
                semantic_field=item.canonical_field,
                operator=item.raw_filter.operator.value,
                value=_filter_value(item),
            ).model_dump(mode="json")
            for item in query.grounded_filters
            if item.canonical_field is not None
            and item.raw_filter.operator in {
                FilterOperator.GT,
                FilterOperator.GTE,
                FilterOperator.LT,
                FilterOperator.LTE,
            }
        ],
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
            "product_universe",
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
