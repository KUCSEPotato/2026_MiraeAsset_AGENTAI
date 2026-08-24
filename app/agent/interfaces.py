from typing import Protocol, runtime_checkable

from app.domain.models import (
    EvidenceBundle,
    ExecutionResult,
    GroundedQuery,
    ParsedQuery,
    QueryPlan,
    ResolvedQuery,
    RetrievalRecord,
    ValidationResult,
)


class QueryAnalyzer(Protocol):
    async def analyze(self, question: str) -> ParsedQuery: ...


class EntityResolver(Protocol):
    async def resolve(self, query: ParsedQuery) -> ResolvedQuery: ...


class OntologyService(Protocol):
    async def ground(self, query: ResolvedQuery) -> GroundedQuery: ...


class Planner(Protocol):
    async def create_plan(self, query: GroundedQuery) -> QueryPlan: ...


class Executor(Protocol):
    async def execute(self, plan: QueryPlan) -> list[RetrievalRecord]: ...


@runtime_checkable
class ExecutionResultExecutor(Executor, Protocol):
    async def execute_with_result(self, plan: QueryPlan) -> ExecutionResult: ...


class EvidenceBuilder(Protocol):
    async def build(
        self,
        query: GroundedQuery,
        records: list[RetrievalRecord],
        execution_result: ExecutionResult | None = None,
    ) -> EvidenceBundle: ...


class EvidenceValidator(Protocol):
    async def validate(
        self,
        query: GroundedQuery,
        evidence: EvidenceBundle,
    ) -> ValidationResult: ...


class AnswerGenerator(Protocol):
    async def generate(
        self,
        question: str,
        evidence: EvidenceBundle,
        validation: ValidationResult,
    ) -> str: ...


class SafeResponseGenerator(Protocol):
    async def generate(self, validation: ValidationResult) -> str: ...
