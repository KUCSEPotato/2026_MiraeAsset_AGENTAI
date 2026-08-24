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


class DeterministicRuleRouter:
    """Create an RDB-oriented structured plan without producing SQL."""

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        inputs = structured_query_inputs(query)
        if inputs["result_limit"] is not None:
            inputs["limit"] = inputs["result_limit"]
        covered = [
            item.constraint_id
            for item in query.semantic_constraints
            if item.status is not ConstraintStatus.UNSUPPORTED
        ]
        return QueryPlan(
            planner=PlannerType.RULE,
            steps=[
                QueryStep(
                    step_id="rdb-search",
                    source=RetrievalSource.RDB,
                    operation=QueryOperation.SEARCH_PRODUCTS,
                    inputs=inputs,
                    covers_constraint_ids=covered,
                )
            ],
            constraint_coverage_required=bool(query.semantic_constraints),
        )
