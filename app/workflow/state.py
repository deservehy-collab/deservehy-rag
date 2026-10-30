import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class WorkflowState(TypedDict, total=False):
    conversation_id: int
    question: str
    resolved_question: str
    previous_question: str | None
    messages: Annotated[list[BaseMessage], add_messages]
    context_plan: dict
    current_message_id: int
    intent: str
    intent_confidence: float
    route: str
    queries: list[str]
    order: dict | None
    pending: dict | None
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
