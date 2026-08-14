import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from app.agent.exceptions import AgentUnavailableError
from app.agent.service import AnswerService, get_answer_service
from app.schemas.api import AnswerResponse

router = APIRouter(tags=["evaluation"])
logger = logging.getLogger(__name__)


@router.get("/answer", response_model=AnswerResponse)
async def answer(
    question_id: Annotated[str, Query(min_length=1)],
    question: Annotated[str, Query(min_length=1)],
    service: Annotated[AnswerService, Depends(get_answer_service)],
) -> AnswerResponse:
    if not question_id.strip():
        raise HTTPException(status_code=422, detail="question_id must not be blank")
    if not question.strip():
        raise HTTPException(status_code=422, detail="question must not be blank")

    request_id = str(uuid4())
    try:
        result = await service.answer(question=question)
    except AgentUnavailableError as exc:
        logger.warning(
            "agent unavailable",
            extra={"request_id": request_id, "question_id": question_id},
            exc_info=True,
        )
        result = exc.to_result()

    return AnswerResponse(
        question_id=question_id,
        question=question,
        retrieved_context=result.retrieved_context,
        think_trace=result.think_trace,
        answer=result.answer,
    )

