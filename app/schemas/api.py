from pydantic import BaseModel


class AnswerResponse(BaseModel):
    question_id: str
    question: str
    retrieved_context: str
    think_trace: str
    answer: str

