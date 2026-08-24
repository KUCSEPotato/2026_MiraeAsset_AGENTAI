from app.domain.models import GroundedQuery, PlannerType, QueryPlan
from app.planning.interfaces import (
    QueryPlanValidator,
    RoutingChecker,
    RuleRouter,
    SupervisorPlanner,
)


class QueryPlanner:
    """Coordinate routing and validated one-shot structured planning."""

    def __init__(
        self,
        *,
        routing_checker: RoutingChecker,
        rule_router: RuleRouter,
        supervisor_planner: SupervisorPlanner,
        plan_validator: QueryPlanValidator,
    ) -> None:
        self._routing_checker = routing_checker
        self._rule_router = rule_router
        self._supervisor_planner = supervisor_planner
        self._plan_validator = plan_validator

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        decision = self._routing_checker.check(query)
        if decision.route is PlannerType.RULE:
            plan = await self._rule_router.create_plan(query)
        else:
            plan = await self._supervisor_planner.create_plan(query)
        plan = plan.model_copy(update={"routing_reasons": decision.reasons})
        return self._plan_validator.validate(plan, query)

