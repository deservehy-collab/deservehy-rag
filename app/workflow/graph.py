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
    # Langfuse 客户端而不是 handler：handler 要 trace_id，只有进了 turn_trace 才知道（T4）。
    langfuse: object = None
    # 低置信入池后的自动排空触发器（FlywheelTrigger）；None＝不触发，池行留待脚本排空。
    flywheel: object = None


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

    async def _drive(self, conversation_id: int, graph_input, config, *, turn_intent=None):
        """一轮对话 = 一条 trace：这里开根 span、把 turn.config 并进 LangGraph config。

        handler 绑在**本轮** trace_id 上，所以它只能在 `turn_trace` 里现建；`_config` 因此保持
        只拼 thread_id（`get_state` / `update_state` 不该带 callbacks）。观测关闭时 turn 为
        None，config 一个键都不加——不许出现「开观测 / 关观测」两种 config 形状。

        `turn_intent`（`(intent, confidence)`）只由 `resume` 传：从 interrupt 续跑时 intent 节点
        不会重跑，这一轮的根行否则就没有 intent，T16 按 traceId 归 token 时那笔（往往整段对话
        最大的一笔）就成了「未归因」。checkpoint 里没有 intent 就传 None——**不编默认值**，
        空着比假数据好；T16 会把这类根行单列为未归因。
        """
        from app.observability.tracing import set_intent_on_trace, turn_trace

        # 根 span 的生命周期 = 这个异步生成器的生命周期：`with` 要等生成器收尾（耗尽 / aclose /
        # 被取消）才退出。所以调用方必须关闭或取消它，否则根 span 永不结束、trace 永不导出；
        # `CustomerService._stream` 的泵任务在 finally 里 cancel 并等它收尾，已守住这条不变量。
        with turn_trace(self.deps.langfuse, conversation_id) as turn:
            if turn is not None:
                config = {**config, **turn.config}
                if turn_intent is not None:
                    # 必须在这里调：此刻 contextvar 指向本轮根 span，写进去的就是这一轮的根行。
                    # 返回值不参与判据（best-effort，tracing 内部已兜住异常）。
                    set_intent_on_trace(*turn_intent)
            stream = self.graph.astream(graph_input, config,
                                        stream_mode=CUSTOM_UPDATES, durability="sync")
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
                   "actions": [], "offer": None,
                   # 每轮清空：否则业务路由没检索时，回复行会写上上一轮知识路由的快照，
                   # 👎 事后回捞到的就是别人的证据。
                   "retrieval_snapshot": []}
        async for item in self._drive(conversation_id, payload, self._config(conversation_id)):
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
        # 这一轮不会重跑 intent 节点，所以把上一轮定下的意图从 checkpoint 里带过来写回本轮根行。
        # `if intent` 是给旧 checkpoint 的：没有就传 None（本轮根行留空 = T16 的「未归因」），
        # 绝不编一个默认意图——那会让报表凭空多出一笔假归因。
        intent = snapshot.values.get("intent")
        confidence = snapshot.values.get("intent_confidence")
        turn_intent = (intent, float(confidence)) if intent else None
        async for item in self._drive(conversation_id, Command(resume=decision),
                                      self._config(conversation_id), turn_intent=turn_intent):
            yield item

    async def get_state(self, conversation_id: int):
        return await self.graph.aget_state(self._config(conversation_id))

    async def update_state(self, conversation_id: int, values: dict):
        return await self.graph.aupdate_state(self._config(conversation_id), values)
