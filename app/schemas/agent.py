from pydantic import BaseModel


class AgentResult(BaseModel):
    retrieved_context: str
    think_trace: str
    answer: str

