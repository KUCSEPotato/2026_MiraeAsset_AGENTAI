"""Deterministic logical capability checks before execution planning.

Static admission is deliberately distinct from data availability: retrievers
must still select a READY snapshot and evidence must prove every requested fact.
This module never infers that data exists just because a canonical field exists.
"""

from app.data.metric_capabilities import (
    MetricCapabilityRegistry,
    PREF01_RETURN_CONTRACTS,
    RISK_GRADE_UNVERIFIED_REASON,
)
from app.domain.models import FilterOperator, GroundedQuery, GroundingStatus, ScalarUnit
from app.graph.mapping import GraphMappingRegistry
from app.ontology.runtime_mapping import TeamOntologyRuntimeMapping
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.predicates import structured_predicate
from app.planning.semantic_ir import SemanticQueryIR
from app.planning.serialization import structured_query_inputs


class SemanticCapabilityValidator:
    """Admit only grounded operators backed by existing runtime contracts."""

    def validate(self, query: GroundedQuery, ir: SemanticQueryIR) -> None:
        del ir  # Review artifact; authority remains the original grounded query.
        parsed = query.parsed_query
        errors: list[str] = []
        runtime = TeamOntologyRuntimeMapping()
        fields = {item.canonical_field: item for item in runtime.fields}
        # Compatibility names are admitted only if an actual graph mapping exists.
        graph = GraphMappingRegistry(version="canonical-v2")
        legacy_graph = GraphMappingRegistry(version="legacy")
        allowed_fields = set(query.canonical_fields.values())

        for item in (*query.grounded_filters, *query.grounded_sort, *query.grounded_requested_fields):
            field = item.canonical_field
            if item.status is not GroundingStatus.RESOLVED or field is None:
                errors.append("unresolved_structured_field")
                continue
            mapping = fields.get(field)
            if mapping is None:
                errors.append(f"unknown_canonical_field:{field}")
                continue
            if hasattr(item, "raw_filter"):
                raw = item.raw_filter
                if field == "product.expense_ratio":
                    errors.append("unsupported_comparison:expense_ratio_scale_unverified")
                if "filter" not in mapping.operations:
                    errors.append(f"filter_capability_disabled:{field}")
                if raw.operator.value == "contains" and field not in {
                    "product.name", "product.short_name", "product.ticker", "product.isin"
                }:
                    errors.append(f"contains_not_supported:{field}")
                ordered = raw.operator in {FilterOperator.GT, FilterOperator.GTE, FilterOperator.LT, FilterOperator.LTE, FilterOperator.BETWEEN}
                if ordered and "ordered_comparison" not in mapping.operations:
                    errors.append(f"ordering_contract_missing:{field}")
                values = raw.value if isinstance(raw.value, list) else [raw.value]
                if any(getattr(value, "unit", ScalarUnit.NONE) is not ScalarUnit.NONE for value in values):
                    errors.append(f"filter_unit_contract_missing:{field}")
            elif hasattr(item, "raw_sort"):
                if not {"sort", "sort_contract"}.intersection(mapping.operations):
                    errors.append(f"sort_capability_disabled:{field}")
            elif "project" not in mapping.operations:
                errors.append(f"projection_capability_disabled:{field}")

        for relation in query.grounded_relations:
            if relation.canonical_relation == "hasRiskGrade":
                # Risk grades remain available through RDB field projection.
                # Graph traversal must not re-enable risk-based candidate selection.
                errors.append(RISK_GRADE_UNVERIFIED_REASON)
            if relation.status is not GroundingStatus.RESOLVED or not relation.canonical_relation:
                errors.append("unresolved_relation")
                continue
            try:
                graph.get(relation.canonical_relation)
            except ValueError:
                try:
                    legacy_graph.get(relation.canonical_relation)
                except ValueError:
                    errors.append(f"unknown_relation:{relation.canonical_relation}")
            if relation.negated:
                errors.append("negated_relation_execution_unsupported")

        for metric in parsed.metrics:
            if metric.temporal and metric.temporal.operation != "PERIOD_VALUE":
                errors.append("historical_series_unavailable")
            if metric.metric == "RETURN":
                contract = PREF01_RETURN_CONTRACTS.get(metric.canonical_field)
                if contract is None or metric.temporal is None or contract.exact_period != metric.temporal.period:
                    errors.append("metric_period_binding_invalid")
            else:
                expected_field = {
                    "AUM": "product.aum", "EXPENSE_RATIO": "product.expense_ratio",
                    "NAV": "product.nav", "PRICE": "product.price",
                }.get(metric.metric)
                if metric.canonical_field != expected_field or expected_field is None:
                    errors.append("metric_field_binding_invalid")
                if metric.temporal is not None:
                    errors.append("historical_metric_period_unavailable")
            if metric.canonical_field and metric.canonical_field not in allowed_fields:
                errors.append(f"ungrounded_metric_field:{metric.canonical_field}")

        if parsed.group_by:
            errors.append("group_by_execution_unsupported")
        if parsed.aggregation:
            errors.append("aggregation_execution_unsupported")
        if parsed.temporal_constraint:
            errors.append("temporal_snapshot_unsupported")
        if parsed.unparsed_material_spans:
            errors.append("unparsed_material_clause")
        for constraint in parsed.semantic_constraints:
            if constraint.unsupported_reason == "holdings_weight_projection_unavailable":
                errors.append("unsupported_comparison:holdings_weight_projection_unavailable")
        if parsed.comparison and parsed.comparison.fields:
            grounded_raw = {item.raw_text for item in query.grounded_requested_fields}
            grounded_names = {item.canonical_field for item in query.grounded_requested_fields}
            if not set(parsed.comparison.fields).issubset(grounded_raw | grounded_names):
                errors.append("ungrounded_comparison_field")
        structured_predicate(query)
        inputs, _ = MetricCapabilityRegistry().prepare(structured_query_inputs(query))
        errors.extend(f"unsupported_comparison:{reason}" for reason in inputs.get("comparison_unsupported_reasons", []))
        if errors:
            # Preserve material constraint IDs alongside precise capability failures.
            errors.extend(f"unsupported_constraint:{item.constraint_id}" for item in query.semantic_constraints if item.status.value == "unsupported")
            raise UnsupportedQuerySemanticsError(errors)
