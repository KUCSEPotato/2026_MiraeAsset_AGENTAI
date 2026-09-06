from app.domain.models import (
    GroundedQuery,
    PlannerType,
    QueryOperation,
    QueryPlan,
    QueryStep,
    RetrievalSource,
)
from app.planning.serialization import structured_query_inputs
from app.domain.models import ConstraintStatus
from app.data.metric_capabilities import MetricCapabilityRegistry


class DeterministicRuleRouter:
    """Create an RDB-oriented structured plan without producing SQL."""

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        raw_inputs = structured_query_inputs(query)
        registry = MetricCapabilityRegistry()
        inputs, capability_unsupported = registry.prepare(raw_inputs)
        covered = [
            item.constraint_id
            for item in query.semantic_constraints
            if item.status is not ConstraintStatus.UNSUPPORTED
        ]
        groups = inputs.get("comparison_groups", [])
        if groups:
            steps = []
            for group in groups:
                scoped, scoped_unsupported = registry.prepare_comparison_group(
                    raw_inputs, group
                )
                capability_unsupported.extend(scoped_unsupported)
                if scoped["result_limit"] is not None:
                    scoped["limit"] = scoped["result_limit"]
                steps.append(QueryStep(
                    step_id=f"rdb-search-{group['group_id']}",
                    source=RetrievalSource.RDB,
                    operation=QueryOperation.SEARCH_PRODUCTS,
                    inputs=scoped,
                    covers_constraint_ids=covered,
                ))
        else:
            if inputs["result_limit"] is not None:
                inputs["limit"] = inputs["result_limit"]
            steps = [QueryStep(
                step_id="rdb-search",
                source=RetrievalSource.RDB,
                operation=QueryOperation.SEARCH_PRODUCTS,
                inputs=inputs,
                covers_constraint_ids=covered,
            )]
        return QueryPlan(
            planner=PlannerType.RULE,
            steps=steps,
            unsupported_constraint_ids=capability_unsupported,
            constraint_coverage_required=bool(query.semantic_constraints),
        )
