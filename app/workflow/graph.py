import asyncio
import inspect
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.workflow.agent_node import make_agent_node
from app.workflow.confirm_nodes import (agent_selector, decision_selector, make_confirm_ticket_node,
                                        make_execute_write_node, make_write_cancelled_node)
from app.workflow.coref import make_coref_node
from app.workflow.events import CUSTOM_UPDATES, unwrap
from app.workflow.input_nodes import make_intent_node, route_node, route_selector
from app.workflow.knowledge_node import gate_node, gate_selector, make_refuse_node, make_retrieve_node
from app.workflow.policy_nodes import make_order_node, make_policy_retrieve_node, policy_selector
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
    resolver: object = None
    expander: object = None
    mcp: object = None


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
    builder.add_node("coref", _traced("coref", make_coref_node(deps)))
    builder.add_node("intent", _traced("intent", make_intent_node(deps)))
    builder.add_node("route", _traced("route", route_node))
    builder.add_node("retrieve", _traced("retrieve", make_retrieve_node(deps)))
    builder.add_node("policy_order", _traced("policy_order", make_order_node(deps)))
    builder.add_node("policy_retrieve", _traced("policy_retrieve", make_policy_retrieve_node(deps)))
    builder.add_node("gate", _traced("gate", gate_node))
    builder.add_node("refuse", _traced("refuse", make_refuse_node(deps)))
    builder.add_node("agent", _traced("agent", make_agent_node(deps)))
    builder.add_node("complaint", _traced("complaint", complaint_node))
    builder.add_node("chitchat", _traced("chitchat", chitchat_node))
    builder.add_node("confirm_ticket", _traced("confirm_ticket", make_confirm_ticket_node()))
    builder.add_node("execute_write", _traced("execute_write", make_execute_write_node(deps)))
    builder.add_node("write_cancelled", _traced("write_cancelled", make_write_cancelled_node(deps)))
    builder.add_node("log", _traced("log", make_log_node(deps)))

    builder.add_edge(START, "coref")
    builder.add_edge("coref", "intent")
    builder.add_edge("intent", "route")
    builder.add_conditional_edges("route", route_selector, {
        "knowledge": "retrieve", "policy": "policy_order", "agent": "agent",
        "complaint": "complaint", "chitchat": "chitchat",
    })
    builder.add_conditional_edges("policy_order", policy_selector,
                                  {"retrieve": "policy_retrieve", "log": "log"})
    builder.add_edge("policy_retrieve", "gate")
    builder.add_edge("retrieve", "gate")
    builder.add_conditional_edges("gate", gate_selector, {"refuse": "refuse", "agent": "agent"})
    for name in ("refuse", "complaint", "chitchat"):
        builder.add_edge(name, "log")
    builder.add_conditional_edges("agent", agent_selector,
                                  {"log": "log", "confirm": "confirm_ticket"})
    builder.add_conditional_edges("confirm_ticket", decision_selector,
                                  {"execute": "execute_write", "cancel": "write_cancelled"})
    builder.add_edge("execute_write", "log")
    builder.add_edge("write_cancelled", "log")
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

    async def _drive(self, graph_input, config):
        stream = self.graph.astream(graph_input, config, stream_mode=CUSTOM_UPDATES, durability="sync")
        try:
            async for mode, data in stream:
                for event, payload in unwrap(mode, data):
                    yield event, payload
        finally:
            await stream.aclose()

    async def respond(self, conversation_id: int, question: str, messages: list, *,
                      context_plan: dict | None = None, current_message_id: int | None = None):
        payload = {"conversation_id": conversation_id, "question": question, "messages": messages,
                   "context_plan": context_plan or {}, "current_message_id": current_message_id or 0,
                   "actions": [], "offer": None}
        async for item in self._drive(payload, self._config(conversation_id)):
            yield item

    async def resume(self, conversation_id: int, decision: str):
        snapshot = await self.get_state(conversation_id)
        # 没有挂起的 interrupt 时，Command(resume=...) 会被 LangGraph 当成「新输入从 START 起图」，
        # 而 resume 并没有喂 question/messages，coref 第一行就 KeyError。所以这里自己把关。
        parked = "confirm_ticket" in (snapshot.next or ())
        interrupted = any(getattr(task, "interrupts", None) for task in (snapshot.tasks or ()))
        if not parked and not interrupted:
            logger.info("workflow resume conv=%s 没有待确认的卡片，空操作", conversation_id)
            return
        async for item in self._drive(Command(resume=decision), self._config(conversation_id)):
            yield item

    async def get_state(self, conversation_id: int):
        return await self.graph.aget_state(self._config(conversation_id))

    async def update_state(self, conversation_id: int, values: dict):
        return await self.graph.aupdate_state(self._config(conversation_id), values)
