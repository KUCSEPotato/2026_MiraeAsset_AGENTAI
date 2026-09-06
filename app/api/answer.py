import asyncio
import json
import logging
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.agent.exceptions import AgentUnavailableError
from app.agent.service import AnswerService, get_answer_service
from app.evidence.llm_answer import AnswerGenerationError
from app.schemas.agent import AgentResult
from app.schemas.api import AnswerResponse

router = APIRouter(tags=["evaluation"])
logger = logging.getLogger(__name__)


@router.get("/answer", response_model=AnswerResponse)
async def answer(
    request: Request,
    question_id: Annotated[str, Query(min_length=1)],
    question: Annotated[str, Query(min_length=1)],
    service: Annotated[AnswerService, Depends(get_answer_service)],
) -> AnswerResponse:
    if not question_id.strip():
        raise HTTPException(status_code=422, detail="question_id must not be blank")
    if not question.strip():
        raise HTTPException(status_code=422, detail="question must not be blank")

    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    started = perf_counter()
    timeout_seconds = request.app.state.operational_settings.request_timeout_seconds
    try:
        async with asyncio.timeout(timeout_seconds):
            result = await service.answer(question=question)
    except AgentUnavailableError as exc:
        logger.warning(
            "agent unavailable",
            extra={"request_id": request_id, "question_id": question_id},
            exc_info=True,
        )
        result = exc.to_result()
    except TimeoutError as exc:
        result = _safe_boundary_result(
            reason="request_timeout",
            answer=(
                "요청 처리 시간이 길어져 답변을 완료하지 못했습니다. "
                "잠시 후 다시 시도해 주세요."
            ),
        )
        _log_safe_boundary_failure(
            message="answer request timed out",
            exc=exc,
            request_id=request_id,
            question_id=question_id,
            started=started,
            reason="request_timeout",
        )
    except AnswerGenerationError as exc:
        result = _safe_boundary_result(
            reason="answer_generation_dependency_failure",
            answer=(
                "답변 생성 서비스를 완료하지 못했습니다. "
                "잠시 후 다시 시도해 주세요."
            ),
        )
        _log_safe_boundary_failure(
            message="answer generation dependency failed",
            exc=exc,
            request_id=request_id,
            question_id=question_id,
            started=started,
            reason="answer_generation_dependency_failure",
        )
    except Exception as exc:
        # Do not catch BaseException subclasses such as CancelledError,
        # KeyboardInterrupt, or SystemExit. Evaluation requests still receive a
        # stable envelope for unexpected application failures, while logs retain
        # only allow-listed diagnostics and never expose the exception message.
        result = _safe_boundary_result(
            reason="safe_internal_error",
            answer="요청 처리 중 일시적인 문제가 발생했습니다.",
        )
        _log_safe_boundary_failure(
            message="unexpected answer processing failure",
            exc=exc,
            request_id=request_id,
            question_id=question_id,
            started=started,
            reason="safe_internal_error",
        )

    try:
        return _render_answer_response(
            request=request,
            question_id=question_id,
            question=question,
            result=result,
            request_id=request_id,
            started=started,
        )
    except Exception as exc:
        # Keep the evaluation contract stable even if an otherwise valid service
        # result cannot be summarized or rendered. The fallback construction is
        # intentionally independent of service-owned metadata.
        _log_safe_boundary_failure(
            message="answer response rendering failed",
            exc=exc,
            request_id=request_id,
            question_id=question_id,
            started=started,
            reason="safe_internal_error",
        )
        fallback = _safe_boundary_result(
            reason="safe_internal_error",
            answer="요청 처리 중 일시적인 문제가 발생했습니다.",
        )
        return AnswerResponse(
            question_id=question_id,
            question=question,
            retrieved_context=fallback.retrieved_context,
            think_trace=fallback.think_trace,
            answer=fallback.answer,
        )


def _render_answer_response(
    *,
    request: Request,
    question_id: str,
    question: str,
    result: AgentResult,
    request_id: str,
    started: float,
) -> AnswerResponse:
    trace = _safe_trace(result.think_trace)
    planning = trace.get("planning_summary", {})
    validation = trace.get("validation_summary", {})
    logger.info(
        "answer request completed",
        extra={
            "request_id": request_id,
            "question_id": question_id,
            "runtime_generation": getattr(
                request.app.state, "runtime_health", {}
            ).get("generation", "unknown"),
            "route_type": trace.get("planner", trace.get("status", "unknown")),
            "selected_stores": planning.get("sources", []),
            "candidate_counts": trace.get("execution_cardinality", {}),
            "latency_ms": round((perf_counter() - started) * 1000.0, 3),
            "answerability_reason": validation.get("reason_codes", []),
            "answer_status": trace.get("status"),
            "evidence_count": trace.get("evidence_count", 0),
            "parser_failure_reason": trace.get("query_understanding", {}).get("reason"),
            "http_status": 200,
        },
    )

    return AnswerResponse(
        question_id=question_id,
        question=question,
        retrieved_context=result.retrieved_context,
        think_trace=result.think_trace,
        answer=result.answer,
    )


def _safe_trace(raw_trace: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw_trace)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_boundary_result(*, reason: str, answer: str) -> AgentResult:
    return AgentResult(
        retrieved_context="",
        think_trace=json.dumps(
            {
                "steps": ["api_boundary"],
                "status": "internal_failure",
                "reason": reason,
            },
            ensure_ascii=False,
        ),
        answer=answer,
    )


def _log_safe_boundary_failure(
    *,
    message: str,
    exc: Exception,
    request_id: str,
    question_id: str,
    started: float,
    reason: str,
) -> None:
    logger.error(
        message,
        extra={
            "request_id": request_id,
            "question_id": question_id,
            "latency_ms": round((perf_counter() - started) * 1000.0, 3),
            "error_class": type(exc).__name__,
            "answer_status": "internal_failure",
            "parser_failure_reason": reason,
            "http_status": 200,
        },
    )
