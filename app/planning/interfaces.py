from typing import Protocol

from app.domain.models import GroundedQuery, QueryPlan, RoutingDecision


class RoutingChecker(Protocol):
    def check(self, query: GroundedQuery) -> RoutingDecision: ...


class RuleRouter(Protocol):
    async def create_plan(self, query: GroundedQuery) -> QueryPlan: ...


class SupervisorPlanner(Protocol):
    async def create_plan(self, query: GroundedQuery) -> QueryPlan: ...


class QueryPlanValidator(Protocol):
    def validate(self, plan: QueryPlan, query: GroundedQuery) -> QueryPlan: ...

