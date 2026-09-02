from app.domain.models import (
    GroundedQuery,
    GroundingStatus,
    PlannerType,
    QueryOperation,
    QueryPlan,
    ResolutionStatus,
    RetrievalSource,
)
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.exceptions import UnsupportedQuerySemanticsError


class QueryPlanValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid query plan: " + "; ".join(errors))


class StructuredQueryPlanValidator:
    def __init__(self, metadata: RoutingMetadataRegistry) -> None:
        self._metadata = metadata

    def validate(self, plan: QueryPlan, query: GroundedQuery) -> QueryPlan:
        errors: list[str] = []
        planner = self._as_planner_type(plan.planner)
        if planner is None:
            errors.append("invalid_planner_type")
        if not plan.steps:
            errors.append("empty_plan")

        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            errors.append("duplicate_step_id")
        known_ids = set(step_ids)

        for step in plan.steps:
            source = self._as_source(step.source)
            operation = self._as_operation(step.operation)
            if source is None:
                errors.append(f"invalid_source:{step.step_id}")
            if operation is None:
                errors.append(f"invalid_operation:{step.step_id}")
            if (
                source is not None
                and operation is not None
                and not self._metadata.supports_operation(source, operation)
            ):
                errors.append(f"unsupported_source_operation:{step.step_id}")
            for dependency in step.depends_on:
                if dependency not in known_ids:
                    errors.append(f"missing_dependency:{step.step_id}:{dependency}")
                if dependency == step.step_id:
                    errors.append(f"self_dependency:{step.step_id}")

        if self._has_dependency_cycle(plan):
            errors.append("dependency_cycle")

        errors.extend(self._semantic_safety_errors(plan, query, planner))
        if errors:
            raise QueryPlanValidationError(_deduplicate(errors))
        if plan.constraint_coverage_required:
            semantic_errors = self._constraint_coverage_errors(plan, query)
            if semantic_errors:
                raise UnsupportedQuerySemanticsError(semantic_errors)
        return plan

    @staticmethod
    def _constraint_coverage_errors(
        plan: QueryPlan,
        query: GroundedQuery,
    ) -> list[str]:
        errors: list[str] = []
        constraints = query.semantic_constraints
        unsupported = {
            item.constraint_id
            for item in constraints
            if item.status.value == "unsupported"
        }
        unsupported.update(query.parsed_query.unsupported_constraint_ids)
        unsupported.update(plan.unsupported_constraint_ids)
        errors.extend(f"unsupported_constraint:{item}" for item in sorted(unsupported))
        errors.extend(
            f"unsupported_comparison:{reason}"
            for step in plan.steps
            for reason in step.inputs.get("comparison_unsupported_reasons", [])
            if isinstance(reason, str) and reason
        )
        if query.parsed_query.unparsed_material_spans:
            errors.append("unparsed_material_clause")

        covered = {
            constraint_id
            for step in plan.steps
            for constraint_id in step.covers_constraint_ids
        }
        required = {
            item.constraint_id
            for item in constraints
            if item.required and item.status.value != "unsupported"
        }
        errors.extend(
            f"uncovered_constraint:{item}"
            for item in sorted(required - covered)
        )

        if query.parsed_query.sort and not _ranking_execution_is_guaranteed(plan):
            errors.append("ranking_execution_not_guaranteed")
        return _deduplicate(errors)
    def _semantic_safety_errors(
        self,
        plan: QueryPlan,
        query: GroundedQuery,
        planner: PlannerType | None,
    ) -> list[str]:
        errors: list[str] = []
        allowed_fields = set(query.canonical_fields.values())
        allowed_concepts = {concept.value for concept in query.canonical_concepts}
        allowed_entity_ids = {
            entity.canonical_id
            for entity in query.resolved_entities
            if entity.resolution_status is ResolutionStatus.RESOLVED
            and entity.canonical_id is not None
        }
        for step in plan.steps:
            used_fields = _extract_canonical_fields(step.inputs)
            unknown_fields = used_fields - allowed_fields
            if unknown_fields:
                errors.append(
                    "invented_canonical_field:"
                    + ",".join(sorted(unknown_fields))
                )
            used_concepts = _extract_canonical_concepts(step.inputs)
            unknown_concepts = used_concepts - allowed_concepts
            if unknown_concepts:
                errors.append(
                    "invented_canonical_concept:"
                    + ",".join(sorted(unknown_concepts))
                )
            used_entity_ids = set(step.inputs.get("entity_ids", []))
            used_entity_ids.update(step.inputs.get("source_node_ids", []))
            unknown_entity_ids = used_entity_ids - allowed_entity_ids
            if unknown_entity_ids:
                errors.append(
                    "invented_entity_id:" + ",".join(sorted(unknown_entity_ids))
                )

        if planner is PlannerType.RULE:
            unresolved_grounding = (
                bool(query.unresolved_concepts)
                or query.parsed_query.requires_semantic_search
                or bool(query.parsed_query.relations)
                or any(
                    item.status is not GroundingStatus.RESOLVED
                    for item in (
                        *query.grounded_filters,
                        *query.grounded_sort,
                        *query.grounded_requested_fields,
                    )
                )
            )
            unresolved_entity = any(
                entity.resolution_status is not ResolutionStatus.RESOLVED
                for entity in query.resolved_entities
            )
            if unresolved_grounding or unresolved_entity:
                errors.append("unsafe_rule_plan_for_unresolved_input")
        return errors

    def _has_dependency_cycle(self, plan: QueryPlan) -> bool:
        graph = {step.step_id: step.depends_on for step in plan.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            for dependency in graph.get(step_id, []):
                if dependency in graph and visit(dependency):
                    return True
            visiting.remove(step_id)
            visited.add(step_id)
            return False

        return any(visit(step_id) for step_id in graph)

    @staticmethod
    def _as_planner_type(value) -> PlannerType | None:
        try:
            return PlannerType(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_source(value) -> RetrievalSource | None:
        try:
            return RetrievalSource(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_operation(value) -> QueryOperation | None:
        try:
            return QueryOperation(value)
        except (TypeError, ValueError):
            return None


def _extract_canonical_fields(inputs: dict) -> set[str]:
    fields: set[str] = set()
    for item in inputs.get("filters", []):
        if isinstance(item, dict) and item.get("canonical_field") is not None:
            fields.add(item["canonical_field"])
    for item in inputs.get("sort", []):
        if isinstance(item, dict) and item.get("canonical_field") is not None:
            fields.add(item["canonical_field"])
    fields.update(inputs.get("requested_fields", []))
    return fields


def _ranking_execution_is_guaranteed(plan: QueryPlan) -> bool:
    """Accept one global RDB ordering followed by an intersection/window.

    Runtime additionally rejects a truncated rankable set before the internal
    transform, so provider branches are never independently ranked/concatenated.
    """

    ranking_steps = [
        step for step in plan.steps
        if step.source is RetrievalSource.INTERNAL
        and step.operation is QueryOperation.RANK_CANDIDATES
    ]
    if not ranking_steps:
        return True
    by_id = {step.step_id: step for step in plan.steps}
    for rank in ranking_steps:
        if not rank.depends_on:
            return False
        upstream = by_id.get(rank.depends_on[0])
        if (
            upstream is None
            or upstream.source is not RetrievalSource.RDB
            or upstream.operation is not QueryOperation.SEARCH_PRODUCTS
            or not upstream.inputs.get("sort")
            or upstream.inputs.get("sort") != rank.inputs.get("sort")
            or not upstream.inputs.get("comparison_contracts")
        ):
            return False
        upstream_limit = upstream.inputs.get("limit")
        output_limit = rank.inputs.get("limit")
        if not isinstance(upstream_limit, int):
            return False
        # A ranking request without Top-N intentionally keeps the complete
        # ordered candidate set.  Runtime's rankable-total guard still rejects
        # a truncated upstream result, so None does not weaken global ranking.
        if output_limit is not None and (
            not isinstance(output_limit, int) or output_limit > upstream_limit
        ):
            return False
    return True


def _extract_canonical_concepts(inputs: dict) -> set[str]:
    concepts = set(inputs.get("product_types", []))
    for item in inputs.get("filters", []):
        if not isinstance(item, dict):
            continue
        value = item.get("canonical_value")
        values = value if isinstance(value, list) else [value]
        concepts.update(
            candidate
            for candidate in values
            if isinstance(candidate, str)
            and candidate.startswith(("FinancialProduct.", "Region.", "AssetType."))
        )
    return concepts


def _deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
