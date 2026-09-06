"""Derive executable outputs without relaxing the population or its ranking.

Original GroundedQuery remains the audit authority. Deferred output IDs are
carried by the plan and re-derived by its validator, never trusted from input.
"""

from dataclasses import dataclass

from app.data.metric_capabilities import MetricCapabilityRegistry, PREF01_RETURN_CONTRACTS
from app.domain.models import (
    ClauseResult, ClauseStatus, ConstraintSemanticType, GroundedQuery,
    GroundingStatus, QueryIntent, ResolutionStatus,
)
from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.serialization import structured_query_inputs


@dataclass(frozen=True)
class PreparedOutputs:
    query: GroundedQuery
    disclosures: list[ClauseResult]


def prepare_outputs(original: GroundedQuery) -> PreparedOutputs:
    parsed = original.parsed_query
    if parsed.comparison and not set(parsed.comparison.fields).issubset({
        *(item.raw_text for item in original.grounded_requested_fields),
        *(item.canonical_field for item in original.grounded_requested_fields if item.canonical_field),
    }):
        raise UnsupportedQuerySemanticsError(["ungrounded_comparison_field"])
    disclosures: list[ClauseResult] = []
    removed: set[str] = set()
    entities = list(original.resolved_entities)
    comparison = parsed.comparison is not None or parsed.intent is QueryIntent.COMPARE_PRODUCTS or bool(parsed.selectors)
    # Unknown members of a fieldwise entity list are output failures. A missing
    # relation target, hard-filter target, or ranked population is never optional.
    factual_list = bool(comparison and parsed.requested_fields and not (
        original.grounded_filters or original.grounded_sort or original.grounded_relations
        or parsed.result_limit or parsed.requires_semantic_search or parsed.aggregation or parsed.group_by
    ))
    if factual_list:
        missing = [item for item in entities if item.entity_type == "product"
                   and item.resolution_status is not ResolutionStatus.RESOLVED]
        for item in missing:
            disclosures.append(ClauseResult(kind="ENTITY", label=item.raw_text,
                constraint_id=item.constraint_id,
                status=ClauseStatus.AMBIGUOUS if item.resolution_status is ResolutionStatus.AMBIGUOUS else ClauseStatus.MISSING,
                reason="entity_not_resolved"))
            if item.constraint_id:
                removed.add(item.constraint_id)
        entities = [item for item in entities if item not in missing]
    if parsed.selectors:
        if not factual_list:
            raise UnsupportedQuerySemanticsError(["peer_selector_population_unverified"])
        for selector in parsed.selectors:
            disclosures.append(ClauseResult(kind="SELECTOR", label=selector.raw_text,
                constraint_id=selector.constraint_id, status=ClauseStatus.UNSUPPORTED,
                reason="peer_selector_unverified"))
            if selector.constraint_id:
                removed.add(selector.constraint_id)
    if (disclosures and not any(item.entity_type == "product" and item.canonical_id
                               and item.resolution_status is ResolutionStatus.RESOLVED for item in entities)):
        raise UnsupportedQuerySemanticsError(["no_resolved_factual_anchor"])

    scoped = original.model_copy(update={"resolved_entities": entities})
    inputs = structured_query_inputs(scoped)
    mapping = {item.canonical_field: item for item in TeamOntologyRuntimeMapping().fields}
    fields = []
    removed_fields = set()
    for field in original.grounded_requested_fields:
        constraint = next((item for item in parsed.semantic_constraints
                           if item.semantic_type is ConstraintSemanticType.REQUESTED_FIELD
                           and item.payload.get("field") == field.raw_text), None)
        metric = next((item for item in parsed.metrics if item.constraint_id is not None
                       and constraint is not None and item.constraint_id == constraint.constraint_id), None)
        capability = mapping.get(field.canonical_field)
        reason = None
        if field.status is not GroundingStatus.RESOLVED or capability is None or "project" not in capability.operations:
            reason = "projection_unavailable"
        if constraint and constraint.unsupported_reason:
            reason = constraint.unsupported_reason
        if metric and metric.temporal and metric.temporal.operation != "PERIOD_VALUE":
            reason = "historical_series_unavailable"
        if field.canonical_field in PREF01_RETURN_CONTRACTS:
            _, unavailable = MetricCapabilityRegistry().comparison_contract(field.canonical_field, inputs)
            reason = reason or unavailable
        if reason:
            removed_fields.add(field.raw_text)
            if constraint:
                removed.add(constraint.constraint_id)
            disclosures.append(ClauseResult(label=field.raw_text, field=field.canonical_field,
                constraint_id=constraint.constraint_id if constraint else None,
                status=ClauseStatus.AMBIGUOUS if field.status is GroundingStatus.AMBIGUOUS else ClauseStatus.UNSUPPORTED,
                reason=reason))
        else:
            fields.append(field)

    if removed_fields and not fields and not original.grounded_sort:
        raise UnsupportedQuerySemanticsError(["no_supported_output_requirements", "unresolved_structured_field"])
    defer_comparison = False
    if comparison and not original.grounded_sort:
        compared = (inputs.get("comparison") or {}).get("fields", [])
        unavailable = [MetricCapabilityRegistry().comparison_contract(field, inputs)[1] for field in compared]
        defer_comparison = bool(disclosures or any(unavailable))
        if defer_comparison:
            if not any(item.entity_type == "product" and item.canonical_id
                       and item.resolution_status is ResolutionStatus.RESOLVED for item in entities):
                raise UnsupportedQuerySemanticsError([
                    f"unsupported_comparison:{reason}" for reason in unavailable if reason
                ] or ["no_resolved_factual_anchor"])
            disclosures.append(ClauseResult(kind="COMPARISON", label="상품 간 비교",
                status=ClauseStatus.UNSUPPORTED if any(unavailable) else ClauseStatus.MISSING,
                reason=next((reason for reason in unavailable if reason), "comparison_incomplete")))
    metrics = [item for item in parsed.metrics if item.constraint_id not in removed]
    if not any(item.temporal and item.temporal.operation != "PERIOD_VALUE" for item in metrics):
        removed.update(item.constraint_id for item in parsed.semantic_constraints
                       if item.unsupported_reason == "historical_metric_series_unavailable")
    hard_ids = {
        *(item.raw_filter.constraint_id for item in original.grounded_filters),
        *(item.raw_sort.constraint_id for item in original.grounded_sort),
        *(item.constraint_id for item in original.grounded_relations),
        *(item.constraint_id for item in (parsed.result_limit, parsed.product_universe,
                                          parsed.aggregation, parsed.group_by, parsed.temporal_constraint) if item),
    } - {None}
    if removed & hard_ids:
        raise UnsupportedQuerySemanticsError(["output_clause_overlaps_hard_constraint"])

    def constraints(values):
        result = []
        for item in values:
            if item.constraint_id in removed:
                continue
            if defer_comparison and item.semantic_type is ConstraintSemanticType.INTENT:
                item = item.model_copy(update={"payload": {"intent": QueryIntent.SEARCH_PRODUCT.value}})
            result.append(item)
        return result

    execution_parsed = parsed.model_copy(update={
        "entities": [item for item in parsed.entities if item.constraint_id not in removed],
        "selectors": [],
        "requested_fields": [item for item in parsed.requested_fields if item not in removed_fields],
        "metrics": metrics,
        "comparison": None if defer_comparison else parsed.comparison,
        "intent": QueryIntent.SEARCH_PRODUCT if defer_comparison else parsed.intent,
        "semantic_constraints": constraints(parsed.semantic_constraints),
        "unsupported_constraint_ids": [item for item in parsed.unsupported_constraint_ids if item not in removed],
    })
    query = scoped.model_copy(update={
        "parsed_query": execution_parsed,
        "grounded_requested_fields": fields,
        "semantic_constraints": constraints(original.semantic_constraints),
        "unresolved_concepts": [item for item in original.unresolved_concepts if item not in removed_fields],
    })
    # No mutation to filters, Boolean tree, sort, TopK, universe, relations, or
    # their identities is permitted here. All stay subject to existing gates.
    return PreparedOutputs(query, disclosures)
