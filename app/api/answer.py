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

    request_id = str(uuid4())
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
        logger.error(
            "answer request timed out",
            extra={
                "request_id": request_id,
                "question_id": question_id,
                "latency_ms": round((perf_counter() - started) * 1000.0, 3),
                "error_class": type(exc).__name__,
                "http_status": 504,
            },
        )
        raise HTTPException(status_code=504, detail="answer request timed out") from exc
    except AnswerGenerationError as exc:
        logger.error(
            "answer generation dependency failed",
            extra={
                "request_id": request_id,
                "question_id": question_id,
                "latency_ms": round((perf_counter() - started) * 1000.0, 3),
                "error_class": type(exc).__name__,
                "http_status": 503,
            },
        )
        raise HTTPException(
            status_code=503, detail="answer generation dependency unavailable"
        ) from exc

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
