import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from app.workflow.agent_node import make_agent_node
from app.workflow.input_nodes import coref_node, make_intent_node, route_node, route_selector
from app.workflow.knowledge_node import gate_node, gate_selector, make_refuse_node, make_retrieve_node
from app.workflow.reply_nodes import chitchat_node, complaint_node, make_log_node
from app.workflow.state import WorkflowState

logger = logging.getLogger(__name__)


@dataclass
class WorkflowDeps:
    model: object
    settings: object
    store: object
    knowledge: object = None
    pool: object = None
    selfcheck: object = None
    classifier: object = None


def _traced(name, node):
    async def wrapped(state):
        logger.info("workflow node=%s conv=%s", name, state.get("conversation_id"))
        result = node(state)
        if inspect.isawaitable(result):
            result = await result
        return result

    return wrapped


def build_graph(deps: WorkflowDeps, checkpointer):
    builder = StateGraph(WorkflowState)
    builder.add_node("coref", _traced("coref", coref_node))
    builder.add_node("intent", _traced("intent", make_intent_node(deps)))
    builder.add_node("route", _traced("route", route_node))
    builder.add_node("retrieve", _traced("retrieve", make_retrieve_node(deps)))
    builder.add_node("gate", _traced("gate", gate_node))
    builder.add_node("refuse", _traced("refuse", make_refuse_node(deps)))
    builder.add_node("agent", _traced("agent", make_agent_node(deps)))
    builder.add_node("complaint", _traced("complaint", complaint_node))
    builder.add_node("chitchat", _traced("chitchat", chitchat_node))
    builder.add_node("log", _traced("log", make_log_node(deps)))

    builder.add_edge(START, "coref")
    builder.add_edge("coref", "intent")
    builder.add_edge("intent", "route")
    builder.add_conditional_edges("route", route_selector, {
        "knowledge": "retrieve", "agent": "agent", "complaint": "complaint", "chitchat": "chitchat",
    })
    builder.add_edge("retrieve", "gate")
    builder.add_conditional_edges("gate", gate_selector, {"refuse": "refuse", "agent": "agent"})
    for name in ("refuse", "agent", "complaint", "chitchat"):
        builder.add_edge(name, "log")
    builder.add_edge("log", END)
    return builder.compile(checkpointer=checkpointer)


@asynccontextmanager
async def open_checkpointer(path: str):
    if path == ":memory:":
        yield InMemorySaver()
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(target)) as saver:
        yield saver


class Workflow:
    def __init__(self, *, deps: WorkflowDeps, checkpointer):
        self.deps = deps
        self.graph = build_graph(deps, checkpointer)
        # 同一会话的「跑一轮」与「建工单」共用一把锁：否则工单读到旧 offer 后回写，
        # 会把新一轮 coref 已经清掉的 offer 复活；反过来也可能被新一轮覆盖而丢 ticket_no。
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, conversation_id: int) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())

    @property
    def knowledge(self):
        return self.deps.knowledge

    @knowledge.setter
    def knowledge(self, value):
        self.deps.knowledge = value

    @property
    def selfcheck(self):
        return self.deps.selfcheck

    @selfcheck.setter
    def selfcheck(self, value):
        self.deps.selfcheck = value

    @property
    def classifier(self):
        return self.deps.classifier

    @classifier.setter
    def classifier(self, value):
        self.deps.classifier = value

    @staticmethod
    def _config(conversation_id: int) -> dict:
        return {"configurable": {"thread_id": str(conversation_id)}}

    async def respond(self, conversation_id: int, question: str, messages: list):
        payload = {"conversation_id": conversation_id, "question": question, "messages": messages,
                   "actions": [], "offer": None}
        stream = self.graph.astream(payload, self._config(conversation_id),
                                    stream_mode="custom", durability="sync")
        try:
            async for item in stream:
                yield item
        finally:
            await stream.aclose()

    async def get_state(self, conversation_id: int):
        return await self.graph.aget_state(self._config(conversation_id))

    async def update_state(self, conversation_id: int, values: dict):
        return await self.graph.aupdate_state(self._config(conversation_id), values)
