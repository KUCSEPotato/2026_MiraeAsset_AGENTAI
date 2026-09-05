from app.domain.models import GroundedQuery, PlannerType, QueryPlan
from app.planning.interfaces import (
    QueryPlanValidator,
    RoutingChecker,
    RuleRouter,
    SupervisorPlanner,
)
from app.planning.capabilities import SemanticCapabilityValidator
from app.planning.semantic_ir import build_semantic_ir
from app.planning.output_requirements import prepare_outputs


class QueryPlanner:
    """Coordinate routing and validated one-shot structured planning."""

    def __init__(
        self,
        *,
        routing_checker: RoutingChecker,
        rule_router: RuleRouter,
        supervisor_planner: SupervisorPlanner,
        plan_validator: QueryPlanValidator,
        capability_validator: SemanticCapabilityValidator | None = None,
    ) -> None:
        self._routing_checker = routing_checker
        self._rule_router = rule_router
        self._supervisor_planner = supervisor_planner
        self._plan_validator = plan_validator
        self._capability_validator = capability_validator or SemanticCapabilityValidator()

    async def create_plan(self, query: GroundedQuery) -> QueryPlan:
        original = query
        prepared = prepare_outputs(original)
        query = prepared.query
        semantic_ir = build_semantic_ir(query)
        self._capability_validator.validate(query, semantic_ir)
        decision = self._routing_checker.check(query)
        if decision.route is PlannerType.RULE:
            plan = await self._rule_router.create_plan(query)
        else:
            plan = await self._supervisor_planner.create_plan(query)
        plan = plan.model_copy(update={
            "routing_reasons": decision.reasons,
            "semantic_ir": semantic_ir.model_dump(mode="json"),
            "output_disclosures": prepared.disclosures,
        })
        return self._plan_validator.validate(plan, original)
