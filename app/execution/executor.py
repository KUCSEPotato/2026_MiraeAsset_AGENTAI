import asyncio
import time
from datetime import datetime, timezone

from app.domain.models import (
    ExecutionContext,
    ExecutionErrorCode,
    ExecutionResult,
    QueryPlan,
    QueryStep,
    RetrievalRecord,
    RetrievalSource,
    StepExecutionResult,
    StepExecutionStatus,
)
from app.execution.config import ExecutionSettings
from app.execution.transforms import InternalTransformExecutor
from app.retrieval.exceptions import RetrievalError
from app.retrieval.registry import RetrieverRegistry


class QueryExecutor:
    def __init__(
        self,
        *,
        registry: RetrieverRegistry,
        transform_executor: InternalTransformExecutor,
        settings: ExecutionSettings,
    ) -> None:
        self._registry = registry
        self._transform_executor = transform_executor
        self._settings = settings

    async def execute(self, plan: QueryPlan) -> list[RetrievalRecord]:
        return (await self.execute_with_result(plan)).records

    async def execute_with_result(self, plan: QueryPlan) -> ExecutionResult:
        context = ExecutionContext(plan=plan)
        pending = {step.step_id: step for step in plan.steps}
        plan_order = {step.step_id: index for index, step in enumerate(plan.steps)}

        while pending:
            ready = [
                step
                for step in plan.steps
                if step.step_id in pending
                and all(
                    dependency in context.step_results
                    for dependency in step.depends_on
                )
            ]
            if not ready:
                raise RuntimeError("executor received a non-executable query plan")

            runnable: list[QueryStep] = []
            layer_results: dict[str, StepExecutionResult] = {}
            for step in ready:
                failed_dependencies = [
                    dependency
                    for dependency in step.depends_on
                    if context.step_results[dependency].status
                    is not StepExecutionStatus.SUCCESS
                ]
                if failed_dependencies:
                    layer_results[step.step_id] = self._skipped_result(
                        step,
                        failed_dependencies,
                    )
                else:
                    runnable.append(step)

            executed = await self._execute_parallel(runnable, context)
            layer_results.update(
                {result.step_id: result for result in executed}
            )
            for step in sorted(ready, key=lambda item: plan_order[item.step_id]):
                context.step_results[step.step_id] = layer_results[step.step_id]
                pending.pop(step.step_id)

        records = self._collect_final_records(plan, context)
        warnings = [
            f"{result.step_id}:{result.error_code.value}"
            for result in context.step_results.values()
            if result.error_code is not None
        ]
        return ExecutionResult(
            records=records,
            step_results=context.step_results,
            warnings=warnings,
        )

    async def _execute_parallel(
        self,
        steps: list[QueryStep],
        context: ExecutionContext,
    ) -> list[StepExecutionResult]:
        tasks = [
            asyncio.create_task(self._execute_step(step, context)) for step in steps
        ]
        if not tasks:
            return []
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_step(
        self,
        step: QueryStep,
        context: ExecutionContext,
    ) -> StepExecutionResult:
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.step_timeout_seconds):
                if step.source is RetrievalSource.INTERNAL:
                    records = await self._transform_executor.execute(step, context)
                else:
                    retriever = self._registry.get(step.source)
                    records = await retriever.retrieve(step, context)
            normalized = [self._with_provenance(record, step) for record in records]
            return self._result(
                step=step,
                status=StepExecutionStatus.SUCCESS,
                records=normalized,
                started_at=started_at,
                started_clock=started_clock,
            )
        except TimeoutError:
            return self._result(
                step=step,
                status=StepExecutionStatus.TIMED_OUT,
                records=[],
                error_code=ExecutionErrorCode.STEP_TIMEOUT,
                error_message="retrieval step timed out",
                started_at=started_at,
                started_clock=started_clock,
            )
        except RetrievalError as exc:
            return self._result(
                step=step,
                status=StepExecutionStatus.FAILED,
                records=[],
                error_code=exc.error_code,
                error_message=str(exc),
                started_at=started_at,
                started_clock=started_clock,
            )

    @staticmethod
    def _with_provenance(
        record: RetrievalRecord,
        step: QueryStep,
    ) -> RetrievalRecord:
        return record.model_copy(
            deep=True,
            update={
                "step_id": step.step_id,
                "source": step.source.value,
                "metadata": {
                    **record.metadata,
                    "execution_step_id": step.step_id,
                },
            },
        )

    @staticmethod
    def _result(
        *,
        step: QueryStep,
        status: StepExecutionStatus,
        records: list[RetrievalRecord],
        started_at: datetime,
        started_clock: float,
        error_code: ExecutionErrorCode | None = None,
        error_message: str | None = None,
    ) -> StepExecutionResult:
        finished_at = datetime.now(timezone.utc)
        return StepExecutionResult(
            step_id=step.step_id,
            source=step.source,
            status=status,
            records=records,
            error_code=error_code,
            error_message=error_message,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=max(0.0, time.perf_counter() - started_clock),
            dependency_ids=step.depends_on,
        )

    @staticmethod
    def _skipped_result(
        step: QueryStep,
        failed_dependencies: list[str],
    ) -> StepExecutionResult:
        now = datetime.now(timezone.utc)
        return StepExecutionResult(
            step_id=step.step_id,
            source=step.source,
            status=StepExecutionStatus.SKIPPED,
            error_code=ExecutionErrorCode.DEPENDENCY_FAILED,
            error_message=(
                "required dependency failed: " + ",".join(failed_dependencies)
            ),
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
            dependency_ids=step.depends_on,
        )

    @staticmethod
    def _collect_final_records(
        plan: QueryPlan,
        context: ExecutionContext,
    ) -> list[RetrievalRecord]:
        dependencies = {step.step_id: step.depends_on for step in plan.steps}
        superseded_step_ids: set[str] = set()
        for step in plan.steps:
            if step.source is not RetrievalSource.INTERNAL:
                continue
            if (
                context.step_results[step.step_id].status
                is not StepExecutionStatus.SUCCESS
            ):
                continue
            pending_ancestors = list(step.depends_on)
            while pending_ancestors:
                ancestor = pending_ancestors.pop()
                if ancestor in superseded_step_ids:
                    continue
                superseded_step_ids.add(ancestor)
                pending_ancestors.extend(dependencies.get(ancestor, []))
        records: list[RetrievalRecord] = []
        for step in plan.steps:
            result = context.step_results[step.step_id]
            if result.status is not StepExecutionStatus.SUCCESS:
                continue
            if step.step_id in superseded_step_ids:
                continue
            records.extend(result.records)
        return records
