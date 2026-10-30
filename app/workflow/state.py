import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage


class WorkflowState(TypedDict, total=False):
    conversation_id: int
    question: str
    previous_question: str | None
    messages: list[BaseMessage]
    intent: str
    route: str
    retrieval: dict
    evidence: list[dict]
    citations: list[dict]
    reply: str
    actions: list[str]
    finish_reason: str | None
    steps: int
    tool_calls: int
    tokens: int
    stopped_by: str | None
    offer: dict | None
    turn_count: Annotated[int, operator.add]
    last_intent: str
    last_question: str
    started_at: float
