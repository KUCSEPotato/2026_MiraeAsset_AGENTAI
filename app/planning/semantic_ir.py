"""A derived, reviewable logical IR over the existing grounded query contract.

The IR is not a second mutable query and never accepts executable SQL, Cypher,
or LLM-selected stores. Physical planning still consumes the grounded query.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import GroundedQuery, QueryIntent, RetrievalSource
from app.data.metric_capabilities import PREF01_RETURN_CONTRACTS


class SemanticOperatorKind(str, Enum):
    RESOLVE_ENTITY = "ResolveEntity"
    FILTER = "Filter"
    PROJECT_FIELD = "ProjectField"
    RESOLVE_METRIC = "ResolveMetric"
    SORT = "Sort"
    TOP_K = "TopK"
    LIMIT = "Limit"
    COMPARE = "Compare"
    AGGREGATE = "Aggregate"
    TRAVERSE_RELATION = "TraverseRelation"
    TEMPORAL_RESOLVE = "TemporalResolve"
    GROUP_BY = "GroupBy"
    SEMANTIC_SEARCH = "SemanticSearch"


class SemanticOperator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SemanticOperatorKind
    fields: tuple[str, ...] = ()
    constraint_ids: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)
    preferred_source: RetrievalSource | None = None


class SemanticQueryIR(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "semantic-composition-v1"
    operators: tuple[SemanticOperator, ...]
    dataset_snapshot: str | None = None
    mapping_version: str | None = None


def build_semantic_ir(query: GroundedQuery) -> SemanticQueryIR:
    operators: list[SemanticOperator] = []
    parsed = query.parsed_query

    def add(kind, *, fields=(), ids=(), source=None, **parameters):
        operators.append(SemanticOperator(
            kind=kind, fields=tuple(field for field in fields if field),
            constraint_ids=tuple(value for value in ids if value),
            parameters=parameters, preferred_source=source,
        ))

    if query.resolved_entities or parsed.entities:
        add(SemanticOperatorKind.RESOLVE_ENTITY,
            ids=[item.constraint_id for item in query.resolved_entities],
            entities=[item.model_dump(mode="json") for item in query.resolved_entities])
    if parsed.product_types or parsed.product_universe:
        add(SemanticOperatorKind.FILTER, source=RetrievalSource.RDB,
            product_types=parsed.product_types,
            product_universe=parsed.product_universe.model_dump(mode="json")
            if parsed.product_universe else None)
    for relation in query.grounded_relations:
        add(SemanticOperatorKind.TRAVERSE_RELATION,
            ids=[relation.constraint_id], source=RetrievalSource.GRAPH,
            relation=relation.model_dump(mode="json"))
    for item in query.grounded_filters:
        add(SemanticOperatorKind.FILTER, fields=[item.canonical_field],
            ids=[item.raw_filter.constraint_id], source=RetrievalSource.RDB,
            predicate=item.model_dump(mode="json"))
    if parsed.boolean_expression:
        add(SemanticOperatorKind.FILTER, source=RetrievalSource.RDB,
            boolean_expression=parsed.boolean_expression.model_dump(mode="json"))

    metrics = [metric.model_dump(mode="json") for metric in parsed.metrics]
    # Old serialized queries retain their semantics without requiring re-parsing.
    for item in [*query.grounded_sort, *query.grounded_requested_fields]:
        contract = PREF01_RETURN_CONTRACTS.get(item.canonical_field)
        if contract and not any(metric.get("canonical_field") == item.canonical_field for metric in metrics):
            raw = item.raw_sort.field if hasattr(item, "raw_sort") else item.raw_text
            metrics.append({"metric": "RETURN", "canonical_field": item.canonical_field,
                "temporal": {"period": contract.exact_period, "operation": "PERIOD_VALUE",
                    "period_source": "DEFAULT_POLICY" if raw.casefold() in {"수익률", "return"} else "EXPLICIT_QUERY"}})
    for metric in metrics:
        if metric.get("temporal"):
            add(SemanticOperatorKind.TEMPORAL_RESOLVE,
                ids=[metric.get("constraint_id")], **metric["temporal"])
        add(SemanticOperatorKind.RESOLVE_METRIC,
            fields=[metric.get("canonical_field")], ids=[metric.get("constraint_id")],
            source=RetrievalSource.RDB, metric=metric)
    if parsed.temporal_constraint:
        add(SemanticOperatorKind.TEMPORAL_RESOLVE,
            **parsed.temporal_constraint.model_dump(mode="json"))
    fields = list(dict.fromkeys(item.canonical_field for item in query.grounded_requested_fields if item.canonical_field))
    if fields:
        add(SemanticOperatorKind.PROJECT_FIELD, fields=fields, source=RetrievalSource.RDB)
    if parsed.requires_semantic_search or parsed.semantic_terms:
        add(SemanticOperatorKind.SEMANTIC_SEARCH, query_terms=parsed.semantic_terms)
    if parsed.comparison or parsed.intent is QueryIntent.COMPARE_PRODUCTS:
        add(SemanticOperatorKind.COMPARE, fields=fields or [item.canonical_field for item in query.grounded_sort],
            mode="fieldwise", source=RetrievalSource.RDB)
    if parsed.group_by:
        add(SemanticOperatorKind.GROUP_BY, fields=parsed.group_by.fields,
            ids=[parsed.group_by.constraint_id])
    if parsed.aggregation:
        add(SemanticOperatorKind.AGGREGATE, **parsed.aggregation.model_dump(mode="json"))
    for item in query.grounded_sort:
        add(SemanticOperatorKind.SORT, fields=[item.canonical_field],
            ids=[item.raw_sort.constraint_id], source=RetrievalSource.RDB,
            direction=item.raw_sort.direction)
    if parsed.result_limit:
        add(SemanticOperatorKind.TOP_K if parsed.sort else SemanticOperatorKind.LIMIT,
            ids=[parsed.result_limit.constraint_id],
            value=parsed.result_limit.value,
            direction=parsed.sort[0].direction if parsed.sort else None)
    return SemanticQueryIR(operators=tuple(operators), dataset_snapshot=query.dataset_snapshot,
        mapping_version=query.semantic_mapping_version)
